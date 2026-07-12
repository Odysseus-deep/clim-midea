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
        if ticks % 30 == 0:  # tailscale names rarely change, refresh every ~30 min
            await _refresh_ts_names()
        try:
            async with _lock:
                await _refresh()
                row = _sample_row()
                # In the lock: spot a setting changed on the remote/unit and log it,
                # compared to the last known state.
                _note_external_change()
            storage.insert(row)
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


def _state() -> dict:
    return {
        "power": device.power_state,
        "mode": _label(device.operational_mode),
        "target": device.target_temperature,
        "indoor": device.indoor_temperature,
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
            try:
                storage.log_command(int(time.time()), "externe", "externe", changes)
                print(f"[info] external change (remote/app): {changes}")
            except Exception as e:
                print(f"[warn] log external: {e}")
    _last_state = cur


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
    return _state()


@app.get("/capabilities")
async def capabilities():
    return _capabilities()


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
    if not device.power_state:
        return f"The AC is off. It is {device.indoor_temperature:.0f} degrees."
    mode = _label(device.operational_mode)
    return (
        f"The AC is on in {_MODE_SAY.get(mode, mode.lower())} mode, "
        f"set to {device.target_temperature:.0f} degrees. "
        f"It is {device.indoor_temperature:.0f} inside, "
        f"{device.outdoor_temperature:.0f} outside."
    )
