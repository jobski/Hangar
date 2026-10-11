"""Other Hangars this one can see and control (peers): a Mac running MLX next to a Windows PC with CUDA, say.

A peer is a name, the URL other machines reach it at, and its token. They come from config.yaml (`peers:`, read-only)
and from the dashboard/CLI/MCP (kept in data/peers.json). A background loop asks each one for its status every few
seconds; the API proxies any call to a peer at /api/peers/<name>/<path>, so the web UI, CLI and MCP can act on it
exactly as on this machine.
"""
from __future__ import annotations

import json
import re
import socket
import sys
import threading
import time
from pathlib import Path

import httpx

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def machine_name(cfg: dict) -> str:
    return str(cfg["manager"].get("name") or socket.gethostname()).lower()


def machine_info(cfg: dict) -> dict:
    return {"name": machine_name(cfg), "os": {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)}


class Peers:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.file: Path = cfg["manager"]["data_dir"] / "peers.json"
        self.interval = max(3, int(cfg["resources"].get("sample_interval_s", 5)))
        self.lock = threading.Lock()
        self.state: dict[str, dict] = {}
        self.stop_flag = threading.Event()

    # ---- the list
    def _saved(self) -> dict:
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def all(self) -> dict[str, dict]:
        out = {n: {**p, "source": "managed"} for n, p in self._saved().items()}
        for n, p in (self.cfg.get("peers") or {}).items():
            out[str(n).lower()] = {"url": p["url"], "token": p.get("token", ""), "source": "config"}
        for p in out.values():
            p["url"] = p["url"].rstrip("/")
        return out

    def get(self, name: str) -> dict:
        p = self.all().get(name)
        if not p:
            raise KeyError(f"no peer called {name}")
        return p

    def add(self, name: str, url: str, token: str, check: bool = True) -> dict:
        name = name.strip().lower()
        if not NAME.match(name):
            raise ValueError("a peer name is 1-32 lowercase letters, digits, - or _")
        if name == machine_name(self.cfg):
            raise ValueError(f"{name} is this machine's own name")
        if name in (self.cfg.get("peers") or {}):
            raise ValueError(f"{name} is set in config.yaml; change it there")
        url = url.strip().rstrip("/")
        if not re.match(r"^https?://", url):
            url = "http://" + url
        info = self.probe(url, token) if check else None
        saved = self._saved()
        saved[name] = {"url": url, "token": token}
        self.file.write_text(json.dumps(saved, indent=1), encoding="utf-8")
        self.poll_one(name)
        return {"name": name, "url": url, "machine": (info or {}).get("machine")}

    def remove(self, name: str):
        saved = self._saved()
        if name not in saved:
            raise KeyError(f"no UI-managed peer called {name}" + (" (it is in config.yaml)" if name in self.all() else ""))
        del saved[name]
        self.file.write_text(json.dumps(saved, indent=1), encoding="utf-8")
        with self.lock:
            self.state.pop(name, None)

    @staticmethod
    def probe(url: str, token: str) -> dict:
        """Check a peer answers with that token; its status, or a ValueError saying what is wrong."""
        try:
            r = httpx.get(url + "/api/status", headers={"Authorization": f"Bearer {token}"}, timeout=8)
        except httpx.HTTPError as e:
            raise ValueError(f"cannot reach {url}: {type(e).__name__}") from e
        if r.status_code == 401:
            raise ValueError(f"{url} refused the token")
        if r.status_code != 200 or "engines" not in r.text:
            raise ValueError(f"{url} does not look like Hangar (HTTP {r.status_code})")
        return r.json()

    # ---- watching
    def start(self):
        threading.Thread(target=self._loop, name="peers", daemon=True).start()

    def shutdown(self):
        self.stop_flag.set()

    def _loop(self):
        while not self.stop_flag.is_set():
            for name in list(self.all()):
                self.poll_one(name)
            self.stop_flag.wait(self.interval)

    def poll_one(self, name: str):
        p = self.all().get(name)
        if not p:
            return
        t0 = time.time()
        try:
            r = httpx.get(p["url"] + "/api/status", headers={"Authorization": f"Bearer {p['token']}"}, timeout=4)
            if r.status_code == 401:
                raise ValueError("the peer refused our token")
            r.raise_for_status()
            st, err = r.json(), None
        except (httpx.HTTPError, ValueError) as e:
            st, err = None, str(e) if isinstance(e, ValueError) else f"unreachable ({type(e).__name__})"
        with self.lock:
            old = self.state.get(name, {})
            self.state[name] = {"online": st is not None, "error": err, "status": st or old.get("status"),
                                "seen": time.time() if st else old.get("seen"), "latency_ms": round((time.time() - t0) * 1000)}

    def public(self) -> list[dict]:
        out = []
        for name, p in sorted(self.all().items()):
            with self.lock:
                s = dict(self.state.get(name) or {"online": False, "error": "not checked yet"})
            st = s.pop("status", None) or {}
            out.append({"name": name, "url": p["url"], "source": p["source"], **s,
                        "machine": st.get("machine") or {"name": name}, "summary": summary(st) if st else None})
        return out


def summary(st: dict) -> dict:
    """The few numbers the fleet view needs from a status."""
    r = st.get("resources") or {}
    gpus = r.get("gpus") or []
    running = []
    for e in st.get("engines") or []:
        run = e.get("running")
        if run:
            w = (run.get("stats") or {}).get("window") or {}
            g = (w.get("1m") or {}).get("gen_tps") or (w.get("5m") or {}).get("gen_tps")
            running.append({"engine": e["name"], "profile": run["profile"], "state": run["state"], "port": e["port"],
                            "gen_tps": g})
    return {"running": running, "engines": len(st.get("engines") or []),
            "vram_used_mib": sum(g.get("vram_used_mib") or 0 for g in gpus),
            "vram_total_mib": sum(g.get("vram_total_mib") or 0 for g in gpus),
            "ram_used_mib": (r.get("ram_total_mib") or 0) - (r.get("ram_available_mib") or 0),
            "ram_total_mib": r.get("ram_total_mib") or 0, "gpu": gpus[0]["name"] if gpus else None,
            "job": (st.get("job") or {}).get("title")}
