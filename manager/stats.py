"""Usage history in SQLite: token counts per minute, run sessions, measured resource use, and an event log.

Engines expose cumulative counters (llama.cpp's names). The collector turns them into deltas - a counter that went
down means the engine restarted - and adds them up per minute, so rows stay few and any period can be summed.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
  minute INTEGER NOT NULL, engine TEXT NOT NULL, profile TEXT NOT NULL,
  prompt_tokens REAL DEFAULT 0, cached_tokens REAL DEFAULT 0, gen_tokens REAL DEFAULT 0,
  prompt_seconds REAL DEFAULT 0, gen_seconds REAL DEFAULT 0, requests REAL DEFAULT 0,
  PRIMARY KEY (minute, engine, profile));
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, engine TEXT, profile TEXT, started REAL, ready REAL, stopped REAL,
  stop_reason TEXT, peak_vram_mib REAL, peak_ram_mib REAL, load_seconds REAL, adopted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, kind TEXT, engine TEXT, profile TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""

FIELDS = ("prompt_tokens", "cached_tokens", "gen_tokens", "prompt_seconds", "gen_seconds", "requests")
# counter name -> usage field
COUNTERS = {
    "llamacpp:prompt_tokens_total": "prompt_tokens",
    "llamacpp:prompt_tokens_cached_total": "cached_tokens",
    "llamacpp:tokens_predicted_total": "gen_tokens",
    "llamacpp:prompt_seconds_total": "prompt_seconds",
    "llamacpp:tokens_predicted_seconds_total": "gen_seconds",
    "strata:requests_total": "requests",
}


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).lastrowid

    # ---- key/value (last update check times, learned resource use ...)
    def get(self, key, default=None):
        r = self.q("SELECT value FROM kv WHERE key=?", (key,))
        return r[0]["value"] if r else default

    def put(self, key, value):
        self.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, str(value)))

    # ---- events
    def event(self, kind, message, engine=None, profile=None, level="info"):
        self.x("INSERT INTO events(ts,level,kind,engine,profile,message) VALUES(?,?,?,?,?,?)",
               (time.time(), level, kind, engine, profile, message))

    def events(self, limit=100):
        return self.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # ---- sessions
    def session_start(self, engine, profile, adopted=False) -> int:
        return self.x("INSERT INTO sessions(engine,profile,started,adopted) VALUES(?,?,?,?)",
                      (engine, profile, time.time(), int(adopted)))

    def session_ready(self, sid):
        self.x("UPDATE sessions SET ready=?, load_seconds=?-started WHERE id=?", (time.time(), time.time(), sid))

    def session_peak(self, sid, vram, ram):
        self.x("UPDATE sessions SET peak_vram_mib=MAX(COALESCE(peak_vram_mib,0),?), "
               "peak_ram_mib=MAX(COALESCE(peak_ram_mib,0),?) WHERE id=?", (vram, ram, sid))

    def session_stop(self, sid, reason):
        self.x("UPDATE sessions SET stopped=?, stop_reason=? WHERE id=? AND stopped IS NULL", (time.time(), reason, sid))

    def close_dangling_sessions(self, keep: set[int]):
        """Sessions left open by a manager that stopped without seeing their end."""
        for r in self.q("SELECT id FROM sessions WHERE stopped IS NULL"):
            if r["id"] not in keep:
                self.session_stop(r["id"], "unknown (manager was not running)")

    def recent_sessions(self, engine, limit=20) -> list[dict]:
        """An engine's latest runs, newest first."""
        return self.q("SELECT profile, started, ready, stopped, stop_reason FROM sessions WHERE engine=? "
                      "ORDER BY id DESC LIMIT ?", (engine, limit))

    def learned(self, profile) -> dict | None:
        """Measured peak use over the last 5 runs that got ready."""
        rows = self.q("SELECT peak_vram_mib v, peak_ram_mib r, load_seconds l FROM sessions WHERE profile=? AND "
                      "ready IS NOT NULL AND peak_vram_mib IS NOT NULL ORDER BY id DESC LIMIT 5", (profile,))
        if not rows:
            return None
        return {"vram_mib": max(r["v"] or 0 for r in rows), "ram_mib": max(r["r"] or 0 for r in rows),
                "load_seconds": sum(r["l"] or 0 for r in rows) / len(rows), "runs": len(rows)}

    # ---- usage
    def add_usage(self, minute: int, engine: str, profile: str, d: dict):
        cols = ",".join(FIELDS)
        vals = [d.get(f, 0.0) for f in FIELDS]
        upd = ",".join(f"{f}={f}+excluded.{f}" for f in FIELDS)
        self.x(f"INSERT INTO usage(minute,engine,profile,{cols}) VALUES(?,?,?,{','.join('?' * len(FIELDS))}) "
               f"ON CONFLICT(minute,engine,profile) DO UPDATE SET {upd}", (minute, engine, profile, *vals))

    # ---- reports
    @staticmethod
    def period_start(period: str, now: float | None = None) -> float:
        n = dt.datetime.fromtimestamp(now or time.time())
        day = n.replace(hour=0, minute=0, second=0, microsecond=0)
        if period in ("day", "today"):
            s = day
        elif period == "week":
            s = day - dt.timedelta(days=day.weekday())
        elif period == "month":
            s = day.replace(day=1)
        elif period == "year":
            s = day.replace(month=1, day=1)
        elif period in ("24h", "7d", "30d", "365d"):
            hours = {"24h": 24, "7d": 168, "30d": 720, "365d": 8760}[period]
            return (now or time.time()) - hours * 3600
        elif period in ("all", "all-time", "alltime"):
            return 0.0
        else:
            raise ValueError(f"unknown period {period!r}: day, week, month, year, all, 24h, 7d, 30d or 365d")
        return s.timestamp()

    def summary(self, period: str = "day") -> dict:
        since = self.period_start(period)
        now = time.time()
        sums = ",".join(f"SUM({f}) {f}" for f in FIELDS)
        by = self.q(f"SELECT engine, profile, {sums} FROM usage WHERE minute*60 >= ? GROUP BY engine, profile",
                    (since,))
        sess = self.q("SELECT engine, profile, started, ready, stopped FROM sessions WHERE COALESCE(stopped, ?) >= ?",
                      (now, since))
        up: dict[str, float] = {}
        runs: dict[str, int] = {}
        for s in sess:
            a, b = max(s["started"], since), (s["stopped"] or now)
            up[s["profile"]] = up.get(s["profile"], 0) + max(0.0, b - a)
            runs[s["profile"]] = runs.get(s["profile"], 0) + 1
        profiles = {}
        for r in by:
            profiles[r["profile"]] = {**{f: r[f] or 0 for f in FIELDS}, "engine": r["engine"]}
        for p in up:
            profiles.setdefault(p, {**{f: 0 for f in FIELDS}, "engine": next(
                (s["engine"] for s in sess if s["profile"] == p), None)})
        for p, v in profiles.items():
            v["uptime_seconds"] = up.get(p, 0.0)
            v["runs"] = runs.get(p, 0)
            _rates(v)
        total = {f: sum(v[f] for v in profiles.values()) for f in FIELDS}
        total["uptime_seconds"] = _union_seconds([(max(s["started"], since), s["stopped"] or now) for s in sess])
        total["runs"] = len(sess)
        _rates(total)
        return {"period": period, "since": since, "total": total, "profiles": profiles}

    def series(self, bucket: str = "day", count: int = 30) -> list[dict]:
        """Token totals per hour/day/week/month for charts, oldest first, empty buckets included."""
        now = dt.datetime.now()
        starts = []
        if bucket == "hour":
            cur = now.replace(minute=0, second=0, microsecond=0)
            starts = [cur - dt.timedelta(hours=i) for i in range(count)]
        elif bucket == "day":
            cur = now.replace(hour=0, minute=0, second=0, microsecond=0)
            starts = [cur - dt.timedelta(days=i) for i in range(count)]
        elif bucket == "week":
            cur = now.replace(hour=0, minute=0, second=0, microsecond=0)
            cur -= dt.timedelta(days=cur.weekday())
            starts = [cur - dt.timedelta(weeks=i) for i in range(count)]
        elif bucket == "month":
            y, m = now.year, now.month
            for _ in range(count):
                starts.append(dt.datetime(y, m, 1))
                y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        elif bucket == "year":
            starts = [dt.datetime(now.year - i, 1, 1) for i in range(count)]
        else:
            raise ValueError("bucket: hour, day, week, month or year")
        starts = sorted(starts)
        edges = [s.timestamp() for s in starts] + [time.time() + 1]
        rows = self.q(f"SELECT minute, {','.join(FIELDS)} FROM usage WHERE minute*60 >= ?", (edges[0],))
        out = [{"start": edges[i], **{f: 0.0 for f in FIELDS}} for i in range(len(starts))]
        import bisect
        for r in rows:
            i = bisect.bisect_right(edges, r["minute"] * 60) - 1
            if 0 <= i < len(out):
                for f in FIELDS:
                    out[i][f] += r[f] or 0
        return out


def merge_summaries(period: str, parts: list[tuple[str, dict]]) -> dict:
    """Several machines' summary() added up. Profiles become machine/profile; run time is the machines' run times
    added (two machines busy for an hour is two hours)."""
    total = {f: 0.0 for f in FIELDS} | {"uptime_seconds": 0.0, "runs": 0}
    profiles = {}
    for machine, s in parts:
        for k in total:
            total[k] += (s.get("total") or {}).get(k) or 0
        for pid, v in (s.get("profiles") or {}).items():
            profiles[f"{machine}/{pid}"] = {**v, "machine": machine, "profile": pid}
    _rates(total)
    return {"period": period, "total": total, "profiles": profiles}


def merge_series(parts: list[list[dict]]) -> list[dict]:
    """Several machines' series() added bucket by bucket. They line up when the machines share a time zone; a
    machine elsewhere has its days start at its own midnight, so its numbers land in the bucket at the same place."""
    if not parts:
        return []
    out = [dict(x) for x in parts[0]]
    for ser in parts[1:]:
        for i, x in enumerate(ser[-len(out):] if len(ser) >= len(out) else ser):
            j = i + max(0, len(out) - len(ser))
            for f in FIELDS:
                out[j][f] = (out[j].get(f) or 0) + (x.get(f) or 0)
    return out


def _rates(v: dict):
    v["gen_tps"] = v["gen_tokens"] / v["gen_seconds"] if v.get("gen_seconds") else None
    v["prompt_tps"] = v["prompt_tokens"] / v["prompt_seconds"] if v.get("prompt_seconds") else None


def _union_seconds(spans):
    total, end = 0.0, None
    for a, b in sorted(spans):
        if end is None or a > end:
            total += b - a
            end = b
        elif b > end:
            total += b - end
            end = b
    return total


class CounterTracker:
    """Turns an engine's cumulative counters into per-scrape deltas."""

    def __init__(self, count_first: bool):
        self.last: dict[str, float] | None = None
        self.count_first = count_first      # True when we started the engine (its counters began at 0)

    def delta(self, cur: dict[str, float]) -> dict:
        out = {}
        if self.last is None and not self.count_first:
            self.last = cur
            return {}
        prev = self.last or {}
        for name, field in COUNTERS.items():
            if name not in cur:
                continue
            v, p = cur[name], prev.get(name, 0.0)
            d = v - p if v >= p else v       # went down: the engine restarted
            if d > 0:
                out[field] = out.get(field, 0.0) + d
        self.last = cur
        return out
