"""HomeKit bridge for the Midea AC.

Exposes the AC as a native Apple Home accessory (HeaterCooler service) so it shows
up in the Home app, answers Siri, and can be shared with the household.

The bridge runs INSIDE the FastAPI process, on the same asyncio loop, and shares the
one AC connection through the injected read_state (in-memory state, refreshed by the
logger) and apply_fields (applies under the lock). It never opens a second LAN
session; the AC only accepts one.

HomeKit lives on the LAN (Bonjour/mDNS discovery), so the driver advertises on the
LAN interface, not the tailnet. HomeKit's own pairing crypto is the security, so
exposing it to the LAN is fine (unlike the raw control API).
"""
import asyncio

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_AIR_CONDITIONER

HK_TO_MODE = {0: "AUTO", 1: "HEAT", 2: "COOL"}
MODE_TO_HK = {"AUTO": 0, "HEAT": 1, "COOL": 2}

HK_INACTIVE, HK_IDLE, HK_HEATING, HK_COOLING = 0, 1, 2, 3

# The AC reports "1" for SILENT fan (it does not return the enum name). Same
# normalization as the web panel.
FAN_ALIAS = {"1": "SILENT"}

# HomeKit can send several settings in a row (mode + temp). We coalesce them into a
# single apply so we don't hammer the single session.
DEBOUNCE = 0.6
POLL = 30  # seconds between characteristic refreshes

EXPOSE_FAN = True
EXPOSE_SWING = True
# The all-or-nothing toggles (iECO, turbo, etc.) are off: on a single accessory the
# Home app ignores each service's name and shows N identical "Climatisation" tiles.
# Those options stay on the web panel. Doing them cleanly in HomeKit would need a
# bridge (one named accessory per option), which would force re-pairing.
EXPOSE_SWITCHES = False


class ClimAccessory(Accessory):
    category = CATEGORY_AIR_CONDITIONER

    def __init__(self, driver, caps: dict, read_state, apply_fields, serial=None, **kw):
        super().__init__(driver, "Clim", **kw)
        self._read_state = read_state           # () -> dict, sync
        self._apply_fields = apply_fields        # async (fields, source) -> dict
        self._caps = caps
        self._pending: dict = {}
        self._timer: asyncio.Task | None = None

        # Concrete fan speeds, ordered, AUTO excluded (no slot on a linear slider;
        # AUTO stays settable from the web panel).
        order = ["SILENT", "LOW", "MEDIUM", "HIGH", "MAX"]
        self._fans = [f for f in order if f in caps.get("fan_speeds", [])]

        lo = caps.get("min_temp") or 16.0
        hi = caps.get("max_temp") or 30.0

        # Unique serial per identity. A fixed serial made iOS think each regenerated
        # accessory was the same one already bound to a home ("already in another
        # home"), which blocked pairing.
        self.set_info_service(
            manufacturer="Midea", model="net_ac_A71A",
            firmware_revision="1.0", serial_number=serial or "clim-midea",
        )

        # Minimal spec-safe core. We do NOT expose Auto: in HomeKit it requires a
        # heat<cool range (two setpoints), but the AC has a single setpoint, so iOS
        # rejects the accessory (pairs then immediately unpairs). Auto stays on the
        # web panel.
        chars = ["CoolingThresholdTemperature", "HeatingThresholdTemperature"]
        if EXPOSE_FAN and self._fans:
            chars.append("RotationSpeed")
        if EXPOSE_SWING and "VERTICAL" in caps.get("swing_modes", []):
            chars.append("SwingMode")
        hc = self.add_preload_service("HeaterCooler", chars=chars)
        hc.is_primary_service = True

        modes = caps.get("modes", [])
        valid = {}
        if "COOL" in modes:
            valid["Cool"] = 2
        if "HEAT" in modes:
            valid["Heat"] = 1
        self._valid_hk = set(valid.values())

        default_state = 2 if "COOL" in modes else 1

        self.c_active = hc.configure_char("Active", value=0, setter_callback=self._set_active)
        self.c_current_state = hc.configure_char("CurrentHeaterCoolerState", value=HK_INACTIVE)
        self.c_target_state = hc.configure_char(
            "TargetHeaterCoolerState", value=default_state,
            valid_values=valid or None, setter_callback=self._set_mode,
        )
        self.c_current_temp = hc.configure_char("CurrentTemperature", value=22.0)
        self.c_cool_temp = hc.configure_char(
            "CoolingThresholdTemperature", value=24.0,
            properties={"minValue": lo, "maxValue": hi, "minStep": 0.5},
            setter_callback=self._set_target,
        )
        self.c_heat_temp = hc.configure_char(
            "HeatingThresholdTemperature", value=22.0,
            properties={"minValue": lo, "maxValue": hi, "minStep": 0.5},
            setter_callback=self._set_target,
        )
        self.c_fan = None
        self.c_swing = None
        if EXPOSE_FAN and self._fans:
            speed_step = round(100 / len(self._fans))
            self.c_fan = hc.configure_char(
                "RotationSpeed", value=speed_step,
                properties={"minValue": 0, "maxValue": 100, "minStep": speed_step},
                setter_callback=self._set_fan,
            )
        if EXPOSE_SWING and "VERTICAL" in caps.get("swing_modes", []):
            self.c_swing = hc.configure_char(
                "SwingMode", value=0, setter_callback=self._set_swing,
            )

        # Optional toggles as separate Switch services. Only what the AC accepts over
        # the LAN: not `eco` (ignored, it is iECO) nor iSense (remote only).
        self._switches: dict[str, object] = {}
        if EXPOSE_SWITCHES:
            switches = [
                ("ieco", "iECO", caps.get("ieco", True)),
                ("turbo", "Turbo", caps.get("turbo", True)),
                ("sleep", "Sleep", True),
                ("out_silent", "Outdoor silent", caps.get("out_silent", True)),
                ("purifier", "Purifier", caps.get("purifier", True)),
                ("freeze_protection", "Freeze protection", caps.get("freeze_protection", True)),
            ]
            for field, label, supported in switches:
                if not supported:
                    continue
                svc = self.add_preload_service("Switch", chars=["Name"])
                svc.configure_char("Name", value=label)
                self._switches[field] = svc.configure_char(
                    "On", value=False,
                    setter_callback=(lambda v, f=field: self._set_switch(f, v)),
                )

    # fan conversions

    def _pct_for_fan(self, name: str) -> int:
        name = FAN_ALIAS.get(name, name)
        if name == "AUTO" or name not in self._fans:
            # AUTO has no slot: show a middle point, never push AUTO back.
            return round(100 * (len(self._fans) // 2 + 1) / len(self._fans)) if self._fans else 50
        return round(100 * (self._fans.index(name) + 1) / len(self._fans))

    def _fan_for_pct(self, pct: float) -> str:
        if not self._fans:
            return "AUTO"
        idx = max(0, min(len(self._fans) - 1, round(pct / 100 * len(self._fans)) - 1))
        return self._fans[idx]

    # setters: stack then apply once

    def _set_active(self, value):
        self._pending["power"] = bool(value)
        self._debounce()

    def _set_mode(self, value):
        self._pending["mode"] = HK_TO_MODE.get(value, "COOL")
        self._pending.setdefault("power", True)  # picking a mode implies on
        self._debounce()

    def _set_target(self, value):
        self._pending["target"] = round(value * 2) / 2
        self._debounce()

    def _set_fan(self, value):
        self._pending["fan"] = self._fan_for_pct(value)
        self._debounce()

    def _set_swing(self, value):
        self._pending["swing"] = "VERTICAL" if value else "OFF"
        self._debounce()

    def _set_switch(self, field, value):
        self._pending[field] = bool(value)
        self._debounce()

    def _debounce(self):
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._timer = self.driver.loop.create_task(self._apply_soon())

    async def _apply_soon(self):
        try:
            await asyncio.sleep(DEBOUNCE)
            pending, self._pending = self._pending, {}
            if not pending:
                return
            after = await self._apply_fields(pending, "homekit")
            self._reflect(after)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # a rejected command must not kill the bridge
            print(f"[warn] homekit apply: {e}")
            self._reflect(self._read_state())  # realign HomeKit on reality

    # push real state into HomeKit. set_value does not fire the setters, so no loop.

    def _reflect(self, st: dict):
        if not st:
            return
        power = bool(st.get("power"))
        self.c_active.set_value(1 if power else 0)

        if st.get("indoor") is not None:
            self.c_current_temp.set_value(st["indoor"])
        target = st.get("target")
        if target is not None:
            self.c_cool_temp.set_value(target)
            self.c_heat_temp.set_value(target)

        mode = st.get("mode")
        hk_mode = MODE_TO_HK.get(mode)
        # Only set modes we actually offer (Cool/Heat). AUTO/DRY/FAN_ONLY have no
        # HeaterCooler target, so keep the previous display.
        if hk_mode is not None and hk_mode in self._valid_hk:
            self.c_target_state.set_value(hk_mode)

        self.c_current_state.set_value(self._current_state(st))

        fan = st.get("fan")
        if self.c_fan is not None and fan is not None:
            self.c_fan.set_value(self._pct_for_fan(fan))
        if self.c_swing is not None:
            self.c_swing.set_value(0 if st.get("swing") in (None, "OFF") else 1)

        for field, char in self._switches.items():
            if st.get(field) is not None:
                char.set_value(bool(st[field]))

    def _current_state(self, st: dict) -> int:
        if not st.get("power"):
            return HK_INACTIVE
        indoor, target = st.get("indoor"), st.get("target")
        mode = st.get("mode")
        if indoor is None or target is None:
            return HK_IDLE
        if mode == "COOL":
            return HK_COOLING if indoor > target + 0.1 else HK_IDLE
        if mode == "HEAT":
            return HK_HEATING if indoor < target - 0.1 else HK_IDLE
        if mode == "AUTO":
            if indoor > target + 0.1:
                return HK_COOLING
            if indoor < target - 0.1:
                return HK_HEATING
        return HK_IDLE  # DRY, FAN_ONLY, or at setpoint

    @Accessory.run_at_interval(POLL)
    async def run(self):
        # The logger already refreshes state every 60s; we just copy it to HomeKit
        # without touching the AC.
        try:
            self._reflect(self._read_state())
        except Exception as e:
            print(f"[warn] homekit poll: {e}")
