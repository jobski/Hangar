"""Usage history in SQLite: token counts per minute, run sessions, measured resource use, an event log, and every
request the gateway (/v1) passed on.

Engines expose cumulative counters (llama.cpp's names). The collector turns them into deltas - a counter that went
down means the engine restarted - and adds them up per minute, so rows stay few and any period can be summed.
"""
from __future__ import annotations

import datetime as dt
import json
import re
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
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, model TEXT, machine TEXT, profile TEXT, client TEXT,
  via TEXT, path TEXT, status INTEGER, stream INTEGER, ttft_s REAL, duration_s REAL, prompt_tokens REAL,
  cached_tokens REAL, gen_tokens REAL, prompt_s REAL, gen_s REAL, error TEXT, caller TEXT);
CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
"""

REQ_FIELDS = ("model", "machine", "profile", "client", "caller", "via", "path", "status", "stream", "ttft_s", "duration_s",
              "prompt_tokens", "cached_tokens", "gen_tokens", "prompt_s", "gen_s", "error")
REQ_GROUPS = {"target": ("machine", "profile"), "client": ("client",), "caller": ("caller",), "model": ("model",),
              "machine": ("machine",)}

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
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(requests)")}
        if "caller" not in cols:                    # a request log made before callers were recorded
            self.db.execute("ALTER TABLE requests ADD COLUMN caller TEXT")
        if "config_key" not in {r[1] for r in self.db.execute("PRAGMA table_info(sessions)")}:
            self.db.execute("ALTER TABLE sessions ADD COLUMN config_key TEXT")
        self.db.execute("CREATE INDEX IF NOT EXISTS sessions_config ON sessions(profile,config_key,id DESC)")
        self.db.execute("CREATE INDEX IF NOT EXISTS sessions_engine ON sessions(engine,id DESC)")

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
    def session_start(self, engine, profile, adopted=False, config_key=None) -> int:
        return self.x("INSERT INTO sessions(engine,profile,started,adopted,config_key) VALUES(?,?,?,?,?)",
                      (engine, profile, time.time(), int(adopted), config_key))

    def session_ready(self, sid):
        self.x("UPDATE sessions SET ready=?, load_seconds=?-started WHERE id=?", (time.time(), time.time(), sid))

    def session_peak(self, sid, vram, ram):
        self.x("UPDATE sessions SET peak_vram_mib=CASE WHEN ? IS NULL THEN peak_vram_mib "
               "ELSE MAX(COALESCE(peak_vram_mib,0),?) END, "
               "peak_ram_mib=MAX(COALESCE(peak_ram_mib,0),?) WHERE id=?", (vram, vram, ram, sid))

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

    def learned(self, profile, config_key=None) -> dict | None:
        """Measured peak use over the last five matching runs that got ready."""
        return self.learned_many([profile], {profile: config_key} if config_key else None).get(profile)

    def learned_many(self, profiles: list[str], config_keys: dict | None = None) -> dict[str, dict]:
        groups = {}
        for offset in range(0, len(profiles), 400):
            batch = profiles[offset:offset + 400]
            marks = ",".join("?" for _ in batch)
            where, args = f"profile IN ({marks})", tuple(batch)
            if config_keys is not None:
                where = " OR ".join("(profile=? AND config_key=?)" for _ in batch)
                args = tuple(v for pid in batch for v in (pid, config_keys[pid]))
            rows = self.q("SELECT * FROM (SELECT profile, peak_vram_mib v, peak_ram_mib r, load_seconds l, "
                          "ROW_NUMBER() OVER (PARTITION BY profile ORDER BY id DESC) n FROM sessions "
                          f"WHERE ({where}) AND ready IS NOT NULL AND peak_vram_mib IS NOT NULL) WHERE n<=5", args)
            for row in rows:
                groups.setdefault(row["profile"], []).append(row)
        return {pid: {"vram_mib": max(r["v"] or 0 for r in rows), "ram_mib": max(r["r"] or 0 for r in rows),
                      "load_seconds": sum(r["l"] or 0 for r in rows) / len(rows), "runs": len(rows)}
                for pid, rows in groups.items()}

    # ---- usage
    def add_usage(self, minute: int, engine: str, profile: str, d: dict):
        cols = ",".join(FIELDS)
        vals = [d.get(f, 0.0) for f in FIELDS]
        upd = ",".join(f"{f}={f}+excluded.{f}" for f in FIELDS)
        self.x(f"INSERT INTO usage(minute,engine,profile,{cols}) VALUES(?,?,?,{','.join('?' * len(FIELDS))}) "
               f"ON CONFLICT(minute,engine,profile) DO UPDATE SET {upd}", (minute, engine, profile, *vals))

    # ---- gateway requests
    def log_request(self, ts: float, **kw):
        cols = [f for f in REQ_FIELDS if f in kw]
        self.x(f"INSERT INTO requests(ts,{','.join(cols)}) VALUES(?{',?' * len(cols)})", (ts, *[kw[c] for c in cols]))

    def prune_requests(self, keep_days: float) -> int:
        """Drop gateway requests older than keep_days (0 keeps everything); how many went."""
        if not keep_days or keep_days <= 0:
            return 0
        with self.lock:
            cutoff = time.time() - keep_days * 86400
            self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            return self.db.execute("DELETE FROM requests WHERE ts < ?", (cutoff,)).rowcount

    def requests(self, limit: int = 50, before: float | None = None) -> list[dict]:
        """The latest gateway requests, newest first (before: only older than this time, for paging)."""
        return self.q("SELECT * FROM requests WHERE ts < ? ORDER BY ts DESC LIMIT ?", (before or time.time() + 1, limit))

    def request_summary(self, period: str = "day", by: str = "target") -> dict:
        """Gateway requests in a period, grouped by target (machine + profile), client, model asked for or machine.
        Speeds only count requests whose engine reported its timing."""
        if by not in REQ_GROUPS:
            raise ValueError(f"by: {', '.join(REQ_GROUPS)}")
        since, keys = self.period_start(period), REQ_GROUPS[by]
        agg = ("COUNT(*) requests, SUM(status >= 400 OR error IS NOT NULL) errors, SUM(prompt_tokens) prompt_tokens, "
               "SUM(cached_tokens) cached_tokens, SUM(gen_tokens) gen_tokens, "
               "SUM(CASE WHEN gen_s > 0 THEN gen_tokens END) timed_gen_tokens, SUM(CASE WHEN gen_s > 0 THEN gen_s END) gen_s, "
               "SUM(CASE WHEN prompt_s > 0 THEN prompt_tokens - COALESCE(cached_tokens, 0) END) timed_prompt_tokens, "
               "SUM(CASE WHEN prompt_s > 0 THEN prompt_s END) prompt_s, AVG(ttft_s) ttft_s, SUM(duration_s) busy_s, MAX(ts) last")
        rows = self.q(f"SELECT {','.join(keys)}, {agg} FROM requests WHERE ts >= ? GROUP BY {','.join(keys)} "
                      "ORDER BY requests DESC", (since,))
        total = self.q(f"SELECT {agg} FROM requests WHERE ts >= ?", (since,))[0]
        for r in rows + [total]:
            _req_rates(r)
        return {"period": period, "since": since, "by": by, "total": total, "rows": rows}

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
        by = self.q(f"SELECT engine, profile, {sums} FROM usage WHERE minute >= ? GROUP BY engine, profile",
                    (int(-(-since // 60)),))
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
        rows = self.q(f"SELECT minute, {','.join(FIELDS)} FROM usage WHERE minute >= ?", (int(-(-edges[0] // 60)),))
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


def _req_rates(r: dict):
    r["gen_tps"] = r["timed_gen_tokens"] / r["gen_s"] if r.get("gen_s") else None
    r["prompt_tps"] = r["timed_prompt_tokens"] / r["prompt_s"] if r.get("prompt_s") else None
    for k in ("requests", "errors", "prompt_tokens", "cached_tokens", "gen_tokens"):
        r[k] = r.get(k) or 0


# a streamed chunk with generated text: OpenAI content/reasoning that is not empty, or an Anthropic content delta
_TOKEN = re.compile(rb'"(?:content|reasoning_content|reasoning)":\s*"[^"]|"type":\s*"content_block_delta"')


class RequestMeter:
    """Token counts and speeds read from a gateway response as it streams past (nothing is held back or buffered,
    except a non-streamed body, which is small). Understands llama.cpp/Strata `timings`, OpenAI `usage` and the
    Anthropic events (message_start / message_delta)."""

    MAX_BODY = 4 << 20

    def __init__(self, t0: float, sse: bool):
        self.t0, self.first, self.last, self.token_at = t0, None, None, None
        self.sse, self.body, self.rest = sse, bytearray(), b""
        self.timings: dict = {}
        self.usage: dict = {}

    def feed(self, chunk: bytes, now: float):
        if not chunk:
            return
        self.first = self.first or now
        self.last = now
        if not self.sse:
            if len(self.body) < self.MAX_BODY:
                self.body += chunk
            return
        lines = (self.rest + chunk).split(b"\n")
        self.rest = lines.pop()
        if len(self.rest) > self.MAX_BODY:
            self.rest = b""
        for line in lines:
            if not line.startswith(b"data:"):
                continue
            if self.token_at is None and _TOKEN.search(line):
                self.token_at = now
            if b'"usage"' in line or b'"timings"' in line:
                self._take(line[5:])

    def _take(self, raw: bytes):
        try:
            o = json.loads(raw)
        except ValueError:
            return
        if not isinstance(o, dict):
            return
        if isinstance(o.get("timings"), dict):
            self.timings.update(o["timings"])
        message = o.get("message")
        u = o.get("usage") or (message.get("usage") if isinstance(message, dict) else None)
        if isinstance(u, dict):
            self.usage.update({k: v for k, v in u.items() if v is not None})

    def result(self) -> dict:
        if self.sse and self.rest.startswith(b"data:"):
            self._take(self.rest[5:])
            self.rest = b""
        if self.body:
            self._take(bytes(self.body))
        # time to the first text or reasoning token (a stream's opening chunks carry none)
        out = {"ttft_s": (self.token_at - self.t0) if self.token_at else None}
        t, u = self.timings, self.usage
        if t.get("predicted_n") is not None:
            out["gen_tokens"] = t["predicted_n"]
            out["cached_tokens"] = t.get("cache_n") or 0
            out["prompt_tokens"] = (t.get("prompt_n") or 0) + out["cached_tokens"]
            out["prompt_s"] = (t.get("prompt_ms") or 0) / 1000 or None
            out["gen_s"] = (t.get("predicted_ms") or 0) / 1000 or None
            return out
        if "completion_tokens" in u or "prompt_tokens" in u:             # OpenAI
            out["gen_tokens"] = u.get("completion_tokens") or 0
            out["prompt_tokens"] = u.get("prompt_tokens") or 0
            out["cached_tokens"] = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        elif "output_tokens" in u or "input_tokens" in u:                # Anthropic: input excludes the cache
            out["cached_tokens"] = u.get("cache_read_input_tokens") or 0
            out["prompt_tokens"] = (u.get("input_tokens") or 0) + out["cached_tokens"] + (u.get("cache_creation_input_tokens") or 0)
            out["gen_tokens"] = u.get("output_tokens") or 0
        start = self.token_at or self.first
        if self.sse and out.get("gen_tokens") and start and self.last and self.last > start:
            out["gen_s"] = self.last - start                             # streamed without timings: first token to last chunk
        return out


def client_name(headers) -> str:
    """Which tool sent a request: X-Hangar-Client, else X-Title, else the product in its User-Agent."""
    for h in ("x-hangar-client", "x-title"):
        if headers.get(h):
            return headers[h].strip()[:40]
    ua = (headers.get("user-agent") or "").strip()
    return (ua.split("/", 1)[0].split(" ", 1)[0] or "unknown")[:40]


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
