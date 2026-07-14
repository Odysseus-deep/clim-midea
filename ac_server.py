#!/usr/bin/env python3
"""
Local control of a Midea air conditioner (LAN protocol, port 6444), over HTTP.

Runs on a machine that is on the AC's LAN. No Midea cloud after the initial
discovery.

Config: a .env file next to this script.
  AC_IP     AC local ip     (required)
  AC_ID     device id       (required)
  AC_TOKEN  V3 token         (required on V3 devices)
  AC_KEY    V3 key           (required on V3 devices)

Run:
  .venv/bin/uvicorn ac_server:app --host 127.0.0.1 --port 8787
"""
import asyncio
import datetime
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from msmart.device import AirConditioner as AC
from msmart.discover import Discover
from pydantic import BaseModel

import analysis
import storage

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

DEV_ID = int(os.environ["AC_ID"])
TOKEN = os.environ.get("AC_TOKEN")
KEY = os.environ.get("AC_KEY")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_optfloat(name: str) -> float | None:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return None


# Location, for the sun-height overlay and the weather forecast. Optional.
AC_LAT = _env_optfloat("AC_LAT")
AC_LON = _env_optfloat("AC_LON")
# Open-Meteo model for the forecast. AROME (Meteo-France, ~1.3km) is best over
# France; set AC_WX_MODEL=best_match elsewhere.
AC_WX_MODEL = os.environ.get("AC_WX_MODEL", "arome_france_hd")

# Autopilot: the AC's own setpoint (consigne) is the ceiling. Keep the room under
# it, turning the AC fully off when it is cool enough (silent, ~0 W, the unit still
# reports temperature), and cooling/holding when the AROME forecast says heat is
# coming. One temperature, no second "comfort" value. Toggleable at runtime via
# POST /autopilot; AC_AUTOPILOT is only the initial state.
AP_PAUSE = _env_float("AC_AP_PAUSE", 3600.0)  # back off this long after a manual command
AP_HYST = _env_float("AC_AP_HYST", 0.5)       # room must exceed setpoint by this to cool
# The room lags outdoor by roughly this (insulation + thermal mass): if the forecast
# peak over the next LOOKAHEAD hours is above setpoint + this, heat is coming and we
# keep cooling instead of idling.
AP_STAYON_MARGIN = _env_float("AC_AP_STAYON_MARGIN", 3.0)
AP_LOOKAHEAD = _env_float("AC_AP_LOOKAHEAD", 4.0)

# Optional calibration for a return-air sensor that reads off the occupied room (some
# sit in the recirculated, partly cooled airflow near the coil). Added to the sensor
# for the autopilot decision and the displayed room temperature, so the room is held
# at the real ceiling. NOT written to the logged sample (kept raw) and cancels in the
# thermal rates (differences). Default 0: on this unit a Fluke thermocouple matched
# the sensor to within its 0.5C quantization, so no correction is needed. Measure your
# own gap with a stabilized air probe before setting this; surfaces and cheap
# thermometers mislead (a wood surface lags the actively cooled air and reads warm).
AC_INDOOR_OFFSET = _env_float("AC_INDOOR_OFFSET", 0.0)

# Runtime state, toggleable via /autopilot. Starts from the env default.
_autopilot = os.environ.get("AC_AUTOPILOT", "0") in ("1", "true", "yes")


# Overridable via env, mostly to exercise failure recovery without waiting minutes.
TIMEOUT = _env_float("AC_TIMEOUT", 12.0)          # AC answers in 1-3s; past that, dead session
SAMPLE_INTERVAL = _env_float("AC_SAMPLE_INTERVAL", 60.0)
MAX_BACKOFF = _env_float("AC_MAX_BACKOFF", 600.0)  # cap on the wait after a failure
FAILS_BEFORE_REDISCOVER = int(_env_float("AC_FAILS_BEFORE_REDISCOVER", 3))
LOCK_TIMEOUT = _env_float("AC_LOCK_TIMEOUT", 30.0)  # past that, an HTTP request gives up

# The IP can change (DHCP lease). Remember the last working one and start from it on
# restart rather than the stale one in .env.
STATE_PATH = HERE / "state.json"


def _load_ip() -> str:
    try:
        return json.loads(STATE_PATH.read_text())["ip"]
    except Exception:
        return os.environ["AC_IP"]


def _save_ip(ip: str) -> None:
    try:
        STATE_PATH.write_text(json.dumps({"ip": ip}))
    except Exception as e:
        print(f"[warn] state.json: {e}")


device = AC(ip=_load_ip(), port=6444, device_id=DEV_ID)
# The AC accepts one LAN session at a time: the logger and HTTP requests share the
# connection, so they share this lock.
_lock = asyncio.Lock()

_health = {"last_ok": None, "last_error": None, "failures": 0, "rediscoveries": 0}

# Last known settings, to spot changes made OUTSIDE our API (remote, Midea app, the
# unit's buttons, a timer). Updated after each command (so we don't confuse them)
# and on every logger sample.
_last_state: dict | None = None

# When the last MANUAL command happened (web, HomeKit, remote). Autopilot backs off
# for AP_PAUSE after it, so it never fights a deliberate action.
_last_manual_ts: float = 0.0

# Autopilot brain, refreshed periodically from backdata: how fast the room warms
# when off / cools when running (C/h), and the AROME forecast bias (model minus
# measured outdoor).
_ap_model: dict = {"warm_rate": None, "cool_rate": None, "bias": 0.0, "ts": 0.0}


async def _auth() -> None:
    if TOKEN and KEY:
        await asyncio.wait_for(device.authenticate(TOKEN, KEY), TIMEOUT)


async def _prepare() -> None:
    # Capabilities + flags to set again after every device rebuild.
    await asyncio.wait_for(device.get_capabilities(), TIMEOUT)
    # Without these two flags, watts and outdoor_fan_speed stay None.
    device.enable_energy_usage_requests = True
    device.enable_group5_data_requests = True


async def _rediscover() -> bool:
    # Find the AC by its device id after an IP change. auto_connect=False keeps it a
    # LAN broadcast, no Midea cloud call; we already have the token/key and they do
    # not change with the IP.
    global device
    try:
        found = await asyncio.wait_for(
            Discover.discover(timeout=5, auto_connect=False), 30.0
        )
    except Exception as e:
        print(f"[warn] rediscover: {e}")
        return False

    match = next((d for d in found if d.id == DEV_ID), None)
    if match is None:
        print(f"[warn] rediscover: device {DEV_ID} not on the LAN")
        return False

    if match.ip == device.ip:
        return False  # same IP, the problem is elsewhere, no rebuild

    print(f"[info] AC found again: {device.ip} -> {match.ip}")
    device = AC(ip=match.ip, port=match.port, device_id=DEV_ID)
    await _auth()
    await _prepare()
    _save_ip(match.ip)
    _health["rediscoveries"] += 1
    return True


async def _call(method_name: str) -> None:
    # Run refresh/apply, with re-auth + one retry if the session dropped. We pass the
    # method NAME because _rediscover swaps the device object; a bound reference would
    # point at the old connection.
    try:
        await asyncio.wait_for(getattr(device, method_name)(), TIMEOUT)
    except Exception:
        await _auth()
        await asyncio.wait_for(getattr(device, method_name)(), TIMEOUT)


class ClimUnreachable(RuntimeError):
    pass


async def _refresh() -> None:
    # Read the state, and FAIL if the AC answered nothing. msmart.refresh() does not
    # raise on a network error: it logs and leaves the object on its defaults (17C,
    # AUTO, sensors None). Without this guard the logger would record phantom samples
    # and think it was healthy. Verified against a dead IP.
    await _call("refresh")
    if not device.online:
        raise ClimUnreachable(f"{device.ip} not responding")


async def _apply() -> None:
    await _call("apply")
    # apply() does not read back real state, so re-read to return the truth.
    await _refresh()
    # Remember what we just set so the logger does not see it as an external change.
    # Done inside the lock (called from an endpoint), so no race with the logger.
    global _last_state
    _last_state = _tracked_state()


def _sample_row() -> dict:
    return {
        "ts": int(time.time()),
        "indoor": device.indoor_temperature,
        "outdoor": device.outdoor_temperature,
        "target": device.target_temperature,
        "power": int(bool(device.power_state)),
        "mode": _label(device.operational_mode),
        "fan": _label(device.fan_speed),
        "watts": device.get_real_time_power_usage(),
        "outdoor_rpm": device.outdoor_fan_speed,
        "follow_me": int(bool(device.follow_me)),
    }


async def _logger_loop() -> None:
    # Sample the state every SAMPLE_INTERVAL and store it. This loop must never die:
    # it survives the AC unplugged, the router rebooting, an IP change. Only process
    # exit stops it. After repeated failures it backs off and looks for the AC again.
    ticks = 0
    while True:
        delay = SAMPLE_INTERVAL
        ticks += 1
        if ticks % 30 == 0:  # every ~30 min
            await _refresh_ts_names()
            await _fetch_forecast()  # keep the forecast fresh for autopilot
        if ticks % 15 == 0:  # refresh the autopilot brain from backdata + forecast
            await asyncio.to_thread(_recompute_ap_model)
        try:
            eco = None
            async with _lock:
                await _refresh()
                row = _sample_row()
                # In the lock: spot a setting changed on the remote/unit and log it,
                # compared to the last known state.
                _note_external_change()
                # Auto eco: cool below the comfort ceiling, idle (off) when cool.
                decision = _autopilot_decision()
                if decision is not None:
                    fields, reason = decision
                    eco_before = _state()
                    _mutate(Settings(**fields))
                    await _apply()
                    eco = (eco_before, _state(), reason)
            storage.insert(row)
            if eco is not None:
                _log_change("auto", "autopilot", eco[0], eco[1])
                print(f"[info] autopilot: {eco[2]}")
            if _health["failures"]:
                print(f"[info] AC reachable again after {_health['failures']} failure(s)")
            _health["last_ok"] = int(time.time())
            _health["failures"] = 0
            _health["last_error"] = None

        except asyncio.CancelledError:
            raise

        except Exception as e:
            _health["failures"] += 1
            _health["last_error"] = f"{type(e).__name__}: {e}"
            n = _health["failures"]
            # Log only at powers of 2: an AC unplugged for a week must not produce
            # ten thousand log lines.
            if n <= 3 or (n & (n - 1)) == 0:
                print(f"[warn] logger (failure {n}): {_health['last_error']}")

            if n >= FAILS_BEFORE_REDISCOVER and n % FAILS_BEFORE_REDISCOVER == 0:
                try:
                    async with _lock:
                        await _rediscover()
                except asyncio.CancelledError:
                    raise
                except Exception as e2:
                    print(f"[warn] rediscover: {e2}")

            # Capped exponential backoff: don't flood the LAN or the logs.
            delay = min(SAMPLE_INTERVAL * 2 ** (n - 1), MAX_BACKOFF)

        await asyncio.sleep(delay)


# HomeKit bridge

HK_ENABLED = os.environ.get("AC_HOMEKIT", "1") not in ("0", "", "false")
HK_PIN = os.environ.get("AC_HOMEKIT_PIN", "842-19-736").encode()
HK_PORT = int(_env_float("AC_HOMEKIT_PORT", 51826))
HK_PERSIST = str(HERE / "homekit.state")

_hk_accessory = None  # referenced by the /pair page


def _lan_ip() -> str | None:
    # LAN interface IP, so HomeKit advertises there and not on the tailnet. HomeKit
    # is discovered by mDNS on the LAN; advertising the tailscale IP would make the
    # accessory unreachable from an iPhone at home.
    import subprocess
    for iface in ("en0", "en1"):
        try:
            out = subprocess.run(["ipconfig", "getifaddr", iface],
                                 capture_output=True, text=True, timeout=4)
            ip = out.stdout.strip()
            if ip:
                return ip
        except Exception:
            pass
    return None


async def _start_homekit(loop):
    # Start the HomeKit driver on the current loop. Best-effort: a failure here must
    # never stop the logger or the API.
    import logging
    import sys
    from pyhap.accessory_driver import AccessoryDriver
    import homekit as hk

    # Under uvicorn the pyhap logger has no handler, so its messages (pairing
    # attempts and their errors) are swallowed. Attach one to stdout, which goes to
    # the log file. Essential for diagnosing pairing.
    hk_log = logging.getLogger("pyhap")
    hk_log.setLevel(logging.DEBUG)
    if not any(getattr(h, "_clim", False) for h in hk_log.handlers):
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s pyhap %(levelname)s: %(message)s"))
        h._clim = True
        hk_log.addHandler(h)
        hk_log.propagate = False

    address = await asyncio.to_thread(_lan_ip)
    # Bind + advertised address = the LAN IP (never the tailnet). We let zeroconf
    # advertise mDNS on ALL interfaces (default): restricting to one broke iOS
    # discovery on a multi-interface host.
    driver = AccessoryDriver(
        loop=loop, address=address, port=HK_PORT,
        persist_file=HK_PERSIST, pincode=HK_PIN,
    )
    global _hk_accessory
    # Serial derived from the generated MAC (unique per identity): keeps iOS from
    # recognizing an accessory already bound to a home via a fixed serial.
    serial = "clim-" + str(driver.state.mac).replace(":", "").lower()
    _hk_accessory = hk.ClimAccessory(
        driver, _capabilities(), read_state=_state, apply_fields=apply_fields,
        serial=serial,
    )
    driver.add_accessory(_hk_accessory)
    await driver.async_start()
    print(f"[info] HomeKit ready on {address or 'auto'}:{HK_PORT} "
          f"(pairing code {HK_PIN.decode()})")
    return driver


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init()
    await _refresh_ts_names()
    await _fetch_forecast()  # so autopilot has the forecast from the first tick
    await asyncio.to_thread(_recompute_ap_model)
    try:
        await _auth()
        await _prepare()
        await _refresh()
        _health["last_ok"] = int(time.time())
    except Exception as e:
        # Start anyway: the AC may be unreachable at boot (power cut, router not ready
        # yet). The logger takes over and goes looking for it.
        print(f"[warn] init AC: {e}")
        _health["last_error"] = f"{type(e).__name__}: {e}"

    task = asyncio.create_task(_logger_loop())

    driver = None
    if HK_ENABLED:
        try:
            driver = await _start_homekit(asyncio.get_running_loop())
        except Exception as e:
            print(f"[warn] HomeKit disabled (could not start): {e}")

    yield

    if driver is not None:
        try:
            await driver.async_stop()
        except Exception as e:
            print(f"[warn] HomeKit stop: {e}")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="clim-midea", lifespan=lifespan)


@asynccontextmanager
async def _talking_to_ac():
    # Take the lock, or give up. An unreachable AC must not hang the request: the
    # logger may hold the lock through its own retries.
    try:
        await asyncio.wait_for(_lock.acquire(), LOCK_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(503, "AC busy or unreachable, try again in a moment")
    try:
        yield
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"AC unreachable: {type(e).__name__}")
    finally:
        _lock.release()


def _label(value) -> str:
    # fan_speed/swing_mode may be an enum or a raw int (custom speed).
    return getattr(value, "name", str(value))


def _room_temp() -> float | None:
    # Calibrated room temperature: the sensor plus AC_INDOOR_OFFSET (see above). None
    # if the sensor has no reading. This is what the autopilot and the UI should use;
    # the raw sensor is only for the logged sample.
    t = device.indoor_temperature
    return None if t is None else round(t + AC_INDOOR_OFFSET, 1)


def _state() -> dict:
    return {
        "power": device.power_state,
        "mode": _label(device.operational_mode),
        "target": device.target_temperature,
        "indoor": _room_temp(),
        "indoor_raw": device.indoor_temperature,
        "indoor_offset": AC_INDOOR_OFFSET,
        "outdoor": device.outdoor_temperature,
        "fan": _label(device.fan_speed),
        "swing": _label(device.swing_mode),
        "eco": device.eco,
        "ieco": device.ieco,
        "turbo": device.turbo,
        "sleep": device.sleep,
        "out_silent": device.out_silent,
        "purifier": device.purifier,
        "freeze_protection": device.freeze_protection,
        "follow_me": device.follow_me,
        "rate_select": _label(device.rate_select),
        "display_on": device.display_on,
        "online": device.online,
    }


def _capabilities() -> dict:
    # What the unit can do. The UI offers nothing else.
    return {
        "modes": [m.name for m in device.supported_operation_modes],
        "fan_speeds": [f.name for f in device.supported_fan_speeds],
        "swing_modes": [s.name for s in device.supported_swing_modes],
        "rate_selects": [r.name for r in device.supported_rate_selects],
        "min_temp": device.min_target_temperature,
        "max_temp": device.max_target_temperature,
        # supports_eco is True on some units but they silently ignore `eco`: the eco
        # mode is actually iECO. Verified by applying both.
        "eco": device.supports_eco,
        "ieco": device.supports_ieco,
        "turbo": device.supports_turbo,
        "out_silent": device.supports_out_silent,
        "purifier": device.supports_purifier,
        "freeze_protection": device.supports_freeze_protection,
        "display": device.supports_display_control,
        "self_clean": device.supports_self_clean,
    }


# Command journal

# State fields whose changes we track. online/indoor/outdoor are excluded: they are
# measurements, not settings, and move on their own.
_TRACKED = ("power", "mode", "target", "fan", "swing", "rate_select", "ieco",
            "turbo", "sleep", "out_silent", "purifier", "freeze_protection",
            "follow_me", "display_on")


def _tracked_state() -> dict:
    # The "settings" subset of current state, to compare sample to sample.
    st = _state()
    return {k: st.get(k) for k in _TRACKED}


def _note_external_change() -> None:
    # Detect a setting changed OUTSIDE our API (remote, Midea app, buttons, timer)
    # and log it with source "externe". Called by the logger after each sample.
    # _last_state is kept current by our own commands (_apply) and here, so any diff
    # not explained by a command is external. Call inside the lock (no race).
    global _last_state
    cur = _tracked_state()
    if _last_state is not None and cur != _last_state:
        # Ignore transitions to/from None (a momentarily unreadable attribute is not
        # a real setting change).
        changes = [c for c in _diff(_last_state, cur)
                   if c["from"] is not None and c["to"] is not None]
        if changes:
            global _last_manual_ts
            _last_manual_ts = time.time()  # remote/app counts as manual
            try:
                storage.log_command(int(time.time()), "externe", "externe", changes)
                print(f"[info] external change (remote/app): {changes}")
            except Exception as e:
                print(f"[warn] log external: {e}")
    _last_state = cur


def _autopilot_decision() -> tuple[dict, str] | None:
    # Decide the autopilot action, or None. The setpoint (consigne) is the ceiling.
    # Cool when the room is above it, OR anticipating the forecast heat with a lead
    # derived from the measured cooling capacity. Idle (off) when cool and no heat
    # ahead. Only manages OFF <-> COOL; leaves HEAT/DRY/FAN/AUTO untouched. Backs off
    # after a manual command.
    if not _autopilot:
        return None
    if time.time() - _last_manual_ts < AP_PAUSE:
        return None
    indoor = _room_temp()
    ceiling = device.target_temperature
    if indoor is None or ceiling is None:
        return None
    on = bool(device.power_state)
    cooling = on and _label(device.operational_mode) == "COOL"
    t_hot, _ = _predict_hot(ceiling)
    now = time.time()
    above = indoor >= ceiling + AP_HYST
    anticipate = t_hot is not None and now >= t_hot - _ap_lead_seconds()
    if not on:
        if above:
            return ({"power": True, "mode": "COOL"}, "room above setpoint")
        if anticipate:
            return ({"power": True, "mode": "COOL"}, "getting ahead of the heat")
        return None
    if cooling and not above and not anticipate and indoor <= ceiling:
        return ({"power": False}, "cool enough, idling")
    return None


def _day_peak() -> tuple[float, float] | None:
    # Highest bias-corrected forecast temp over the next 18h, and when, for the
    # indicator ("35 at 15:00"), even beyond the decision lookahead.
    now = time.time()
    horizon = now + 18 * 3600
    bias = _ap_model["bias"]
    best = None
    for f in _forecast_cache["data"]:
        if now <= f["ts"] <= horizon:
            t = f["temp"] - bias
            if best is None or t > best[0]:
                best = (t, f["ts"])
    return best


def _hhmm(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")


def _autopilot_status() -> dict:
    # What autopilot is doing and why, for the dashboard indicator. Mirrors the same
    # above/anticipate signals as the decision, so the two never disagree.
    if not _autopilot:
        return {"on": False, "text": "off"}
    now = time.time()
    if now - _last_manual_ts < AP_PAUSE:
        mins = int((AP_PAUSE - (now - _last_manual_ts)) / 60) + 1
        return {"on": True, "text": f"paused (manual override), ~{mins} min left"}
    indoor = _room_temp()
    ceiling = device.target_temperature
    if indoor is None or ceiling is None:
        return {"on": True, "text": "waiting for a reading"}
    c = f"{ceiling:.0f}"
    t_hot, _ = _predict_hot(ceiling)
    lead = _ap_lead_seconds()
    above = indoor >= ceiling + AP_HYST
    anticipate = t_hot is not None and now >= t_hot - lead
    dp = _day_peak()
    coming = (f", {dp[0]:.0f}° at {_hhmm(dp[1])}"
              if dp and dp[0] >= ceiling + AP_STAYON_MARGIN else "")
    if above:
        return {"on": True, "text": f"cooling, room above {c}°"}
    if anticipate:
        return {"on": True, "text": f"cooling to hold {c}°{coming}"}
    if t_hot is not None:
        mins = max(0, int((t_hot - lead - now) / 60))
        return {"on": True, "text": f"idle, cooling in ~{mins} min{coming}"}
    if coming:
        return {"on": True, "text": f"idle, cool for now{coming}"}
    return {"on": True, "text": "idle, cool enough"
            + (f" (peak {dp[0]:.0f}°)" if dp else "")}


# tailscale IP -> readable name. Filled at boot then refreshed periodically.
_ts_names: dict[str, str] = {}


async def _refresh_ts_names() -> None:
    # Build the tailscale IP -> name map, best-effort, so the journal shows a name
    # instead of a bare 100.x IP. If tailscale is absent or silent, keep the IPs.
    def _run() -> dict[str, str]:
        import json as _json
        import subprocess
        for exe in ("/opt/homebrew/bin/tailscale", "tailscale"):
            try:
                out = subprocess.run([exe, "status", "--json"], capture_output=True,
                                     text=True, timeout=8)
                data = _json.loads(out.stdout)
                names: dict[str, str] = {}
                peers = list((data.get("Peer") or {}).values()) + [data.get("Self", {})]
                for p in peers:
                    name = (p.get("DNSName") or "").split(".")[0] or p.get("HostName", "")
                    for ip in p.get("TailscaleIPs", []):
                        if name:
                            names[ip] = name
                return names
            except Exception:
                continue
        return {}

    try:
        names = await asyncio.to_thread(_run)
        if names:
            _ts_names.update(names)
    except Exception as e:
        print(f"[warn] tailscale names: {e}")


def _source(request: Request | None) -> str:
    return request.client.host if request and request.client else "?"


def _diff(before: dict, after: dict) -> list[dict]:
    # Only the real state changes, measured on the device (not the requested value).
    changes = []
    for f in _TRACKED:
        a, b = before.get(f), after.get(f)
        if a != b:
            changes.append({"field": f, "from": a, "to": b})
    return changes


def _log_change(source: str, endpoint: str, before: dict, after: dict) -> None:
    # Log the command only if it actually changed something. Called outside the AC
    # lock (the SQLite write must not hold the connection). A no-op command (e.g.
    # ignored `eco`, or re-setting an identical value) leaves no trace, on purpose.
    changes = _diff(before, after)
    if not changes:
        return
    if source != "auto":
        global _last_manual_ts
        _last_manual_ts = time.time()
    try:
        storage.log_command(int(time.time()), source, endpoint, changes)
    except Exception as e:
        print(f"[warn] log_command: {e}")


def _clamp_temp(value: float) -> float:
    lo = device.min_target_temperature or 16.0
    hi = device.max_target_temperature or 30.0
    if not lo <= value <= hi:
        raise HTTPException(400, f"temperature out of range, expected {lo}-{hi} C")
    return round(value * 2) / 2  # the AC only does half degrees


def _parse_mode(mode: str) -> AC.OperationalMode:
    try:
        m = AC.OperationalMode[mode.upper()]
    except KeyError:
        supported = [x.name for x in device.supported_operation_modes]
        raise HTTPException(400, f"invalid mode '{mode}', expected: {supported}")
    if m not in device.supported_operation_modes:
        supported = [x.name for x in device.supported_operation_modes]
        raise HTTPException(400, f"mode '{m.name}' not supported, expected: {supported}")
    return m


def _parse_fan(fan: str) -> AC.FanSpeed:
    try:
        f = AC.FanSpeed[fan.upper()]
    except KeyError:
        supported = [x.name for x in device.supported_fan_speeds]
        raise HTTPException(400, f"invalid fan speed '{fan}', expected: {supported}")
    if f not in device.supported_fan_speeds:
        supported = [x.name for x in device.supported_fan_speeds]
        raise HTTPException(400, f"fan speed '{f.name}' not supported, expected: {supported}")
    return f


@app.get("/status")
async def status():
    async with _talking_to_ac():
        await _refresh()
    return {**_state(), "autopilot": _autopilot_status()}


@app.get("/capabilities")
async def capabilities():
    return _capabilities()


@app.get("/config")
async def config():
    # Non-AC config for the dashboard. lat/lon drive the sun overlay and forecast;
    # indoor_offset shifts the logged (raw) indoor curve to the calibrated room temp.
    return {"lat": AC_LAT, "lon": AC_LON, "indoor_offset": AC_INDOOR_OFFSET}


@app.post("/autopilot")
async def set_autopilot(on: bool = Query(...)):
    # Engage or disengage autopilot at runtime. Disengaging never changes the AC's
    # current state, it just stops managing it.
    global _autopilot
    if on != _autopilot:
        try:
            storage.log_command(int(time.time()), "auto", "autopilot",
                                [{"field": "autopilot", "from": _autopilot, "to": on}])
        except Exception as e:
            print(f"[warn] log_command: {e}")
    _autopilot = on
    print(f"[info] autopilot {'engaged' if on else 'disengaged'}")
    return {"autopilot": _autopilot}


_forecast_cache: dict = {"ts": 0.0, "data": []}


async def _fetch_forecast() -> None:
    # Refresh the hourly outdoor temperature forecast from Open-Meteo (AROME by
    # default). Needs internet; on failure the last good data is kept. Also used by
    # autopilot, so the logger refreshes it even when no dashboard is open.
    if AC_LAT is None or AC_LON is None:
        return
    try:
        import httpx
        params = {"latitude": AC_LAT, "longitude": AC_LON, "hourly": "temperature_2m",
                  "models": AC_WX_MODEL, "forecast_days": 2, "timezone": "UTC"}
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
            j = r.json()
        hourly = j.get("hourly", {})
        out = []
        for iso, temp in zip(hourly.get("time", []), hourly.get("temperature_2m", [])):
            if temp is None:
                continue
            ts = int(datetime.datetime.fromisoformat(iso)
                     .replace(tzinfo=datetime.timezone.utc).timestamp())
            out.append({"ts": ts, "temp": temp})
        if out:
            _forecast_cache["data"] = out
            _forecast_cache["ts"] = time.time()
    except Exception as e:
        print(f"[warn] forecast: {e}")


@app.get("/forecast")
async def forecast():
    if AC_LAT is None or AC_LON is None:
        return {"forecast": []}
    if not _forecast_cache["data"] or time.time() - _forecast_cache["ts"] >= 1800:
        await _fetch_forecast()
    return {"forecast": _forecast_cache["data"], "model": AC_WX_MODEL}


def _forecast_bias() -> float:
    # AROME's recent past-hours forecast minus the outdoor we actually measured,
    # averaged. Positive = AROME runs warm. Subtracted from future temps to de-bias,
    # so anticipation reacts to the real weather, not the model's error.
    fc = _forecast_cache["data"]
    if not fc:
        return 0.0
    now = time.time()
    since = int(now - 6 * 3600)
    meas = [(r["ts"], r["outdoor"]) for r in storage.history(since)
            if r["outdoor"] is not None]
    if not meas:
        return 0.0
    diffs = []
    for f in fc:
        if not since <= f["ts"] <= now:
            continue
        near = min(meas, key=lambda m: abs(m[0] - f["ts"]))
        if abs(near[0] - f["ts"]) <= 1800:
            diffs.append(f["temp"] - near[1])
    return round(sum(diffs) / len(diffs), 2) if len(diffs) >= 2 else 0.0


def _recompute_ap_model() -> None:
    # Refresh the thermal rates (from backdata) and the forecast bias. Cheap enough
    # every ~15 min; the per-tick decision reads this cache.
    try:
        rates = analysis.thermal_rates(storage.history(int(time.time() - 7 * 86400)))
        _ap_model["warm_rate"] = rates["warm_rate"]
        _ap_model["cool_rate"] = rates["cool_rate"]
        _ap_model["cool_by_fan"] = rates["cool_by_fan"]
        _ap_model["bias"] = _forecast_bias()
        _ap_model["ts"] = time.time()
    except Exception as e:
        print(f"[warn] autopilot model: {e}")


def _predict_hot(ceiling: float) -> tuple[float | None, float]:
    # Earliest future time (epoch) at which the bias-corrected forecast outdoor
    # reaches ceiling + STAYON_MARGIN (room would breach the ceiling), within
    # LOOKAHEAD. Also returns the peak corrected temp over that window.
    now = time.time()
    horizon = now + AP_LOOKAHEAD * 3600
    thr = ceiling + AP_STAYON_MARGIN
    bias = _ap_model["bias"]
    t_hot, peak = None, None
    for f in _forecast_cache["data"]:
        if not now <= f["ts"] <= horizon:
            continue
        temp = f["temp"] - bias
        peak = temp if peak is None else max(peak, temp)
        if t_hot is None and temp >= thr:
            t_hot = f["ts"]
    return t_hot, (peak if peak is not None else (device.outdoor_temperature or 0.0))


def _ap_lead_seconds() -> float:
    # How early to start cooling: time to take ~1C off, from the cooling capacity
    # measured for the CURRENT fan speed (a quieter/lower fan cools more slowly), with
    # fallbacks. A 1.5x safety margin covers slower configs (outdoor silent). Bounded.
    fan = _label(device.fan_speed)
    by_fan = _ap_model.get("cool_by_fan") or {}
    cap = by_fan.get(fan) or _ap_model["cool_rate"] or -4.0
    lead = 1.5 * 3600 / abs(cap)
    return max(15 * 60, min(120 * 60, lead))


class Settings(BaseModel):
    # Everything settable in one shot. One apply(), so one round trip to the AC and
    # one avoided beep.
    model_config = {"extra": "forbid"}

    power: bool | None = None
    mode: str | None = None
    target: float | None = None
    fan: str | None = None
    swing: str | None = None
    rate_select: str | None = None
    eco: bool | None = None
    ieco: bool | None = None
    turbo: bool | None = None
    sleep: bool | None = None
    out_silent: bool | None = None
    purifier: bool | None = None
    freeze_protection: bool | None = None
    follow_me: bool | None = None


def _parse_enum(kind, value: str, supported, what: str):
    try:
        v = kind[value.upper()]
    except KeyError:
        raise HTTPException(400, f"invalid {what} '{value}', expected: {[x.name for x in supported]}")
    if v not in supported:
        raise HTTPException(400, f"{what} '{v.name}' not supported, expected: {[x.name for x in supported]}")
    return v


def _mutate(s: Settings) -> None:
    # Set the requested fields on the device object (without applying). May raise
    # HTTPException on an invalid or unsupported setting. Shared between /set and the
    # HomeKit bridge.
    if s.mode is not None:
        device.operational_mode = _parse_mode(s.mode)
    if s.target is not None:
        device.target_temperature = _clamp_temp(s.target)
    if s.fan is not None:
        device.fan_speed = _parse_fan(s.fan)
    if s.swing is not None:
        device.swing_mode = _parse_enum(AC.SwingMode, s.swing, device.supported_swing_modes, "swing")
    if s.rate_select is not None:
        device.rate_select = _parse_enum(AC.RateSelect, s.rate_select,
                                         device.supported_rate_selects, "rate_select")
    if s.eco is not None:
        # supports_eco is True but the unit ignores the command: on this model the
        # eco mode is iECO. Verified by applying both.
        raise HTTPException(400, "'eco' is ignored by this unit, use 'ieco'")
    for name, capable in (
        ("ieco", device.supports_ieco),
        ("turbo", device.supports_turbo),
        ("out_silent", device.supports_out_silent),
        ("purifier", device.supports_purifier),
        ("freeze_protection", device.supports_freeze_protection),
    ):
        val = getattr(s, name)
        if val is None:
            continue
        if not capable:
            raise HTTPException(400, f"'{name}' is not supported by this unit")
        setattr(device, name, val)
    if s.sleep is not None:
        device.sleep = s.sleep
    if s.follow_me is not None:
        # Verified: the AC ignores follow_me over the LAN. iSense is a remote-control
        # function (the remote broadcasts its own temperature); the unit cannot turn
        # it on by itself. We refuse rather than lie.
        raise HTTPException(
            400,
            "iSense can only be set from the remote; the AC ignores it over the "
            "network. Its state is readable in /status.",
        )
    # Last: power on/off after the other settings, so the AC starts in the right mode.
    if s.power is not None:
        device.power_state = s.power
    device.beep = False


async def apply_fields(fields: dict, source: str) -> dict:
    # HomeKit bridge entry point: same validation, lock, read-back and journal as
    # /set. One path to the AC.
    s = Settings(**fields)
    async with _talking_to_ac():
        before = _state()
        _mutate(s)
        await _apply()
        after = _state()
    _log_change(source, "homekit", before, after)
    return after


@app.post("/set")
async def set_settings(s: Settings, request: Request):
    async with _talking_to_ac():
        before = _state()
        _mutate(s)
        await _apply()
        after = _state()
    _log_change(_source(request), "set", before, after)
    return {"ok": True, **after}


@app.post("/display")
async def toggle_display(request: Request):
    # The display cannot be set to a value, only toggled.
    if not device.supports_display_control:
        raise HTTPException(400, "display not controllable on this unit")
    async with _talking_to_ac():
        before = _state()
        await asyncio.wait_for(device.toggle_display(), TIMEOUT)
        await _refresh()
        after = _state()
    _log_change(_source(request), "display", before, after)
    return {"ok": True, **after}


@app.post("/on")
async def turn_on(
    request: Request,
    temp: float = Query(24.0, description="target in C"),
    mode: str = Query("COOL"),
    fan: str | None = Query(None),
):
    async with _talking_to_ac():
        before = _state()
        m = _parse_mode(mode)
        t = _clamp_temp(temp)
        device.power_state = True
        device.beep = False
        device.target_temperature = t
        device.operational_mode = m
        if fan is not None:
            device.fan_speed = _parse_fan(fan)
        await _apply()
        after = _state()
    _log_change(_source(request), "on", before, after)
    return {"ok": True, **after}


@app.post("/off")
async def turn_off(request: Request):
    async with _talking_to_ac():
        before = _state()
        device.power_state = False
        device.beep = False
        await _apply()
        after = _state()
    _log_change(_source(request), "off", before, after)
    return {"ok": True, **after}


@app.post("/temp")
async def set_temp(value: float, request: Request):
    async with _talking_to_ac():
        before = _state()
        device.target_temperature = _clamp_temp(value)
        device.beep = False
        await _apply()
        after = _state()
    _log_change(_source(request), "temp", before, after)
    return {"ok": True, **after}


@app.post("/fan")
async def set_fan(value: str, request: Request):
    async with _talking_to_ac():
        before = _state()
        device.fan_speed = _parse_fan(value)
        device.beep = False
        await _apply()
        after = _state()
    _log_change(_source(request), "fan", before, after)
    return {"ok": True, **after}


@app.post("/mode")
async def set_mode(value: str, request: Request):
    async with _talking_to_ac():
        before = _state()
        device.operational_mode = _parse_mode(value)
        device.beep = False
        await _apply()
        after = _state()
    _log_change(_source(request), "mode", before, after)
    return {"ok": True, **after}


@app.post("/toggle")
async def toggle(
    request: Request,
    temp: float = Query(24.0, description="target applied if turning on"),
    mode: str = Query("COOL", description="mode applied if turning on"),
):
    # Flip the state. For a single-button iOS shortcut.
    async with _talking_to_ac():
        await _refresh()
        before = _state()  # after the refresh: the truth before our command
        turning_on = not device.power_state
        if turning_on:
            device.operational_mode = _parse_mode(mode)
            device.target_temperature = _clamp_temp(temp)
        device.power_state = turning_on
        device.beep = False
        await _apply()
        after = _state()
    _log_change(_source(request), "toggle", before, after)
    return {"ok": True, **after}


@app.get("/commands")
async def get_commands(limit: int = Query(50, ge=1, le=500)):
    # Command journal, most recent first. Does not touch the AC.
    rows = storage.commands(limit)
    for r in rows:
        r["source_name"] = _ts_names.get(r["source"], r["source"])
    return {"commands": rows}


@app.get("/pair", response_class=HTMLResponse)
async def pair_page():
    # HomeKit pairing page: QR + code, openable in a browser.
    paired = bool(json.loads(Path(HK_PERSIST).read_text()).get("paired_clients")) \
        if Path(HK_PERSIST).exists() else False
    pin = HK_PIN.decode()
    if _hk_accessory is None:
        return "<p>HomeKit bridge not started.</p>"
    import base64
    import io
    import pyqrcode
    uri = _hk_accessory.xhm_uri()
    buf = io.BytesIO()
    pyqrcode.create(uri).png(buf, scale=6, quiet_zone=3)
    qr = base64.b64encode(buf.getvalue()).decode()
    status = ("Already paired to a controller. To re-pair, first remove the "
              "accessory in the Home app." if paired else
              "Waiting for pairing.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>HomeKit pairing</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 20px;color:#111}}
@media(prefers-color-scheme:dark){{body{{background:#111;color:#eee}}}}
.code{{font-size:32px;font-weight:700;letter-spacing:2px;font-variant-numeric:tabular-nums}}
img{{background:#fff;padding:14px;border-radius:12px;width:220px;height:220px}}
ol{{padding-left:20px}} li{{margin:6px 0}} .muted{{color:#888;font-size:14px}}</style></head><body>
<h1>Add the AC to Apple Home</h1>
<p class="muted">{status}</p>
<p><img alt="HomeKit QR" src="data:image/png;base64,{qr}"></p>
<p>Pairing code: <span class="code">{pin}</span></p>
<ol>
<li>iPhone on the <b>same WiFi</b> as this machine, <b>Tailscale/VPN off</b>.</li>
<li><b>Home</b> app, <b>+</b>, <b>Add Accessory</b>.</li>
<li>Scan the QR above, <b>or</b> tap <b>"More options"</b> at the bottom (a DIY
accessory only shows up there, not in the big tile), pick the accessory and enter
the code.</li>
</ol>
<p class="muted">Still not showing? The WiFi is probably filtering multicast (client
isolation / guest network). Try the main WiFi, or move the iPhone closer to the
router.</p>
</body></html>"""


@app.get("/health")
async def health():
    # Diagnostics without touching the AC: takes no lock, always answers.
    now = int(time.time())
    age = now - _health["last_ok"] if _health["last_ok"] else None
    return {
        # Healthy as long as a sample succeeded in the last 5 minutes.
        "healthy": age is not None and age < 300,
        "seconds_since_last_sample": age,
        "consecutive_failures": _health["failures"],
        "last_error": _health["last_error"],
        "rediscoveries": _health["rediscoveries"],
        "device_ip": device.ip,
        "autopilot": _autopilot,
        "autopilot_paused": _autopilot and (now - _last_manual_ts) < AP_PAUSE,
        "autopilot_model": {k: _ap_model[k] for k in
                            ("warm_rate", "cool_rate", "cool_by_fan", "bias")},
        **storage.stats(),
    }


def _window(hours: float, start: int | None, end: int | None) -> tuple[int, int | None]:
    # Query bounds. start/end (epoch) win over hours; that is what drag-to-zoom sends.
    if start is not None:
        return start, end
    return int(time.time() - hours * 3600), None


@app.get("/history")
async def get_history(
    hours: float = Query(24.0, gt=0, le=24 * 365),
    start: int | None = Query(None, description="epoch lower bound (wins over hours)"),
    end: int | None = Query(None, description="epoch upper bound"),
    max_points: int = Query(2000, ge=50, le=20000),
):
    since, until = _window(hours, start, end)
    rows = storage.history(since, until)
    return {"samples": storage.downsample(rows, max_points),
            "returned": min(len(rows), max_points), "total": len(rows),
            **storage.stats()}


@app.get("/hysteresis")
async def get_hysteresis(
    hours: float = Query(24.0, gt=0, le=24 * 365),
    start: int | None = Query(None),
    end: int | None = Query(None),
):
    since, until = _window(hours, start, end)
    return analysis.analyse(storage.history(since, until))


@app.get("/chart", response_class=HTMLResponse)
async def chart():
    return (Path(__file__).parent / "chart.html").read_text()


_MODE_SAY = {
    "COOL": "cooling",
    "HEAT": "heating",
    "DRY": "dry",
    "FAN_ONLY": "fan only",
    "AUTO": "auto",
    "SMART_DRY": "smart dry",
}


@app.get("/say", response_class=PlainTextResponse)
async def say():
    # Short sentence, for Siri to read out.
    async with _talking_to_ac():
        await _refresh()
    room = _room_temp()
    if not device.power_state:
        return f"The AC is off. It is {room:.0f} degrees."
    mode = _label(device.operational_mode)
    return (
        f"The AC is on in {_MODE_SAY.get(mode, mode.lower())} mode, "
        f"set to {device.target_temperature:.0f} degrees. "
        f"It is {room:.0f} inside, "
        f"{device.outdoor_temperature:.0f} outside."
    )
