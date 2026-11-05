"""Optional GPUMonitor sidecar on Windows or Linux, matched by PCI bus when available.

GPUMonitor (a separate app on the GPU box) reads power, the enforced power limit and the
kernel-time percentage straight from NVML. Hangar's own `nvidia-smi` path reports kernel time
as "utilization", which reads ~0 % for pure CUDA work and hides how hard a card actually works.
When the sidecar is up, its `load = power / limit` replaces that on the dashboard; otherwise this
is a no-op and nothing changes. Read-only: power-limit control and the deep per-card history stay
in GPUMonitor's own UI, where confirmations and charts belong.

The probe runs on Windows and Linux when configured. A missing or down sidecar is
simply absent (60 s backoff before re-probing), never an error the user has to see.
"""
from __future__ import annotations

import sys
import threading
import time

import httpx

TELEMETRY_TTL = 5.0    # seconds between polls of a healthy sidecar
RETRY_S = 60.0         # backoff before re-probing a missing or down one
TIMEOUT = 3.0

# clock-event reasons that mean the card is being held back against the user's interest;
# a power cap or reduced clocks under load is normal, so those are deliberately left out
_CONCERNING = {"thermal", "HW slowdown", "HW brake"}

_lock = threading.Lock()
_http: httpx.Client | None = None
_state = {"probed": 0.0, "ok": False, "fetched": 0.0, "cards": {}}   # cards keyed by NVML index
_base_url = ""


def configure(base_url: str):
    """Set the sidecar address; an empty string disables it."""
    global _base_url, _http
    with _lock:
        base_url = (base_url or "").rstrip("/")
        if base_url == _base_url:
            return
        if _http is not None:
            _http.close()
            _http = None
        _base_url = base_url
        _state.update(probed=0.0, ok=False, fetched=0.0, cards={})


def _client() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(base_url=_base_url, timeout=TIMEOUT)
    return _http


def telemetry() -> dict[int, dict]:
    """Latest sidecar samples keyed by NVML index; {} when disabled or unavailable."""
    if (sys.platform != "win32" and not sys.platform.startswith("linux")) or not _base_url:
        return {}
    with _lock:
        now = time.monotonic()
        if _state["fetched"] and now - _state["fetched"] < TELEMETRY_TTL:
            return dict(_state["cards"])
        _state["fetched"] = now
        try:
            if not _state["ok"]:
                if _state["probed"] and now - _state["probed"] < RETRY_S:
                    return {}
                _state["probed"] = now
            response = _client().get("/api/snapshot")
            response.raise_for_status()
            snap = response.json()
            age = snap.get("sample_age_ms")
            if isinstance(age, (int, float)) and age > max(15000, 3 * (snap.get("interval_ms") or 250)):
                raise ValueError("sidecar samples are stale")
            cards = {}
            for c in snap.get("cards", []):
                ident = c.get("identity") or {}
                sample = c.get("sample")
                idx = ident.get("index")
                if isinstance(idx, int) and isinstance(sample, dict):
                    cards[idx] = {**sample, "pci_bus_id": ident.get("bus_id"),
                                  "brand": ident.get("brand"), "model": ident.get("model"),
                                  "display_name": ident.get("display_name")}
            # an answer without our shape (some other app on the port) is not a sidecar
            _state["ok"] = bool(cards)
            _state["cards"] = cards
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            _state["ok"] = False
            _state["cards"] = {}
            return {}
        return dict(_state["cards"])


def _bus(value):
    parts = str(value or "").lower().split(":")
    if len(parts) == 3:
        return parts[0].lstrip("0") + ":" + ":".join(parts[1:])
    return ""


def merge(gpus: list[dict]) -> list[dict]:
    """Attach honest load (and the numbers behind it) to the cards the sidecar reports; no-op otherwise.

    A sidecar card adds `load_percent` (power / enforced limit, the honest "how hard"),
    `kernel_percent` (what the plain `load` really measures, kept for the tooltip),
    `power_limit_w`, and `throttling` (only the concerning clock-event reasons)."""
    tele = telemetry()
    if not tele:
        return gpus
    for g in gpus:
        s = tele.get(g.get("index"))
        bus = _bus(g.get("pci_bus_id"))
        if bus and s and _bus(s.get("pci_bus_id")) and _bus(s.get("pci_bus_id")) != bus:
            s = None
        if bus:
            s = next((sample for sample in tele.values() if _bus(sample.get("pci_bus_id")) == bus), s)
        if not s:
            continue
        g["load_percent"] = s.get("load_pct")
        for field in ("brand", "model", "display_name"):
            if s.get(field):
                g[field] = s[field]
        if s.get("display_name"):
            # Older hubs render only name; retain the canonical NVIDIA model in model.
            g["name"] = s["display_name"]
        g["kernel_percent"] = s.get("kernel_pct")
        g["power_limit_w"] = s.get("limit_w")
        g["throttling"] = [w for w in (s.get("throttle") or []) if w in _CONCERNING]
    return gpus


def public() -> dict:
    """Dashboard hint while the sidecar answers: its port, so the UI can point at its own UI on that machine."""
    if not _state["ok"] or not _base_url:
        return {}
    return {"port": httpx.URL(_base_url).port or 8765}
