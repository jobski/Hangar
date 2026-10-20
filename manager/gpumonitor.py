"""Optional GPUMonitor sidecar (Windows only): richer NVIDIA telemetry, merged by PCI bus id.

The probe runs only on win32, so the hub and the Mac never touch it; when the
sidecar is missing or down, the extra readings are simply absent. Read-only —
power control stays in GPUMonitor's own UI (its UAC prompts belong on the PC).
"""
from __future__ import annotations

import sys
import threading
import time

import httpx

BASE_URL = "http://127.0.0.1:8765"
TELEMETRY_TTL = 5.0   # seconds between polls
RETRY_S = 60.0        # backoff before re-probing a missing or down sidecar
TIMEOUT = 3.0

_lock = threading.Lock()
_http: httpx.Client | None = None
_state = {"probed": 0.0, "ok": False, "version": None, "fetched": 0.0, "cards": {}}


def _client() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(base_url=BASE_URL, timeout=TIMEOUT)
    return _http


def bus_key(raw: object) -> str:
    """Normalise a PCI bus id to the sidecar's DDDDDDDD:BB:DD.F form (nvidia-smi's)."""
    s = str(raw or "").strip().upper()
    parts = s.split(":")
    if len(parts) == 3 and parts[0].isdigit() and len(parts[0]) < 8:
        parts[0] = parts[0].zfill(8)
        s = ":".join(parts)
    return s


def telemetry() -> dict[str, dict]:
    """Latest sidecar readings keyed by bus id; {} off Windows or when unavailable."""
    if sys.platform != "win32":
        return {}
    with _lock:
        now = time.monotonic()
        if now - _state["fetched"] < TELEMETRY_TTL:
            return dict(_state["cards"])
        _state["fetched"] = now
        try:
            if not _state["ok"]:
                if now - _state["probed"] < RETRY_S:
                    return {}
                _state["probed"] = now
                info = _client().get("/api/info").json()
                _state["ok"] = info.get("name") == "gpumonitor"
                _state["version"] = info.get("version") if _state["ok"] else None
                if not _state["ok"]:
                    return {}
            data = _client().get("/api/telemetry").json()
            _state["cards"] = {c["bus_id"]: c for c in data.get("cards", [])
                               if isinstance(c, dict) and c.get("bus_id")}
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            _state["ok"] = False
            _state["cards"] = {}
            return {}
        return dict(_state["cards"])


def status() -> dict | None:
    """Availability for /api/status; None off Windows so the key is omitted entirely."""
    if sys.platform != "win32":
        return None
    return {"available": _state["ok"], "version": _state["version"]}


def merge(gpus: list[dict]) -> list[dict]:
    """Attach sidecar readings to cards whose bus id matches; no-op when unavailable."""
    tele = telemetry()
    if not tele:
        return gpus
    for g in gpus:
        extra = tele.get(bus_key(g.get("bus_id")))
        if not extra:
            continue
        limits = extra.get("limits") or {}
        g.update({
            "kernel_percent": extra.get("kernel_pct"),
            "fan_percent": extra.get("fan_pct"),
            "pstate": extra.get("pstate"),
            "sm_clock_mhz": extra.get("sm_clock_mhz"),
            "memory_clock_mhz": extra.get("memory_clock_mhz"),
            "throttle": extra.get("throttle") or [],
            "power_limit_w": limits.get("enforced_w"),
            "power_default_w": limits.get("default_w"),
            "power_min_w": limits.get("min_w"),
            "tm_number": extra.get("tm_number"),
            "gpumonitor": True,
        })
    return gpus
