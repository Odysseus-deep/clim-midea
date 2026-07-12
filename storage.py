"""SQLite storage for samples and the command journal."""
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

_BUNDLED = Path(__file__).parent / "clim.db"


def _resolve_db_path() -> Path:
    # Use AC_DB_PATH only if its volume is actually mounted. Otherwise mkdir would
    # create a ghost folder on the internal disk that gets shadowed when the drive
    # comes back, splitting the data. When unsure, stay local and say so.
    env = os.environ.get("AC_DB_PATH")
    if not env:
        return _BUNDLED
    p = Path(env)
    volume = p.parent.parent  # /Volumes/<vol>/clim/clim.db -> /Volumes/<vol>
    if not volume.exists():
        print(f"[warn] AC_DB_PATH={env} but {volume} is not mounted, using {_BUNDLED}")
        return _BUNDLED
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


DB_PATH = _resolve_db_path()

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts          INTEGER PRIMARY KEY,  -- epoch seconds, UTC
    indoor      REAL,
    outdoor     REAL,
    target      REAL,
    power       INTEGER,              -- 0/1
    mode        TEXT,
    fan         TEXT,
    watts       REAL,                 -- instantaneous power
    outdoor_rpm INTEGER,              -- outdoor unit fan
    follow_me   INTEGER               -- iSense: regulates on the remote's sensor
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);

-- Command journal: who changed what, when. One row per request that actually
-- changed something (no-op commands leave no trace).
CREATE TABLE IF NOT EXISTS commands (
    ts       INTEGER,
    source   TEXT,      -- caller tailscale IP, or "homekit" / "externe"
    endpoint TEXT,
    changes  TEXT       -- JSON: [{"field":.., "from":.., "to":..}, ...]
);
CREATE INDEX IF NOT EXISTS idx_commands_ts ON commands(ts);
"""

# Columns added later. SQLite has no ADD COLUMN IF NOT EXISTS.
MIGRATIONS = [("follow_me", "INTEGER")]


@contextmanager
def connect():
    # `with sqlite3.connect(...)` alone does not close the connection (it only
    # handles the transaction). That leaked a file descriptor per call, up to the
    # launchd limit of 256, after which the db could not be opened at all.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")  # logger writes while /history reads
        with conn:
            yield conn
    finally:
        conn.close()


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(samples)")}
        for name, decl in MIGRATIONS:
            if name not in existing:
                conn.execute(f"ALTER TABLE samples ADD COLUMN {name} {decl}")


def insert(row: dict) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO samples
               (ts, indoor, outdoor, target, power, mode, fan, watts, outdoor_rpm,
                follow_me)
               VALUES (:ts, :indoor, :outdoor, :target, :power, :mode, :fan,
                       :watts, :outdoor_rpm, :follow_me)""",
            row,
        )


def log_command(ts: int, source: str, endpoint: str, changes: list[dict]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO commands (ts, source, endpoint, changes) VALUES (?, ?, ?, ?)",
            (ts, source, endpoint, json.dumps(changes)),
        )


def commands(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM commands ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["changes"] = json.loads(d["changes"])
        out.append(d)
    return out


def history(since_ts: int, until_ts: int | None = None) -> list[dict]:
    with connect() as conn:
        if until_ts is None:
            rows = conn.execute(
                "SELECT * FROM samples WHERE ts >= ? ORDER BY ts", (since_ts,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts",
                (since_ts, until_ts),
            ).fetchall()
    return [dict(r) for r in rows]


def downsample(rows: list[dict], max_points: int) -> list[dict]:
    # Decimate rather than average: averaging would smooth the power fronts, and
    # those are exactly where the information is (compressor start/stop). The
    # hysteresis analysis works on the raw rows.
    if len(rows) <= max_points:
        return rows
    step = len(rows) / max_points
    out = [rows[int(i * step)] for i in range(max_points)]
    if out[-1] is not rows[-1]:
        out[-1] = rows[-1]
    return out


def stats() -> dict:
    with connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n, MIN(ts) first, MAX(ts) last FROM samples"
        ).fetchone()
    return dict(r)
