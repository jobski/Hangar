"""Other Hangars this one can see and control (peers): a Mac running MLX next to a Windows PC with CUDA, say.

A peer is a name, the URL other machines reach it at, and its token. They come from config.yaml (`peers:`, read-only)
and from the dashboard/CLI/MCP (kept in data/peers.json). A background loop asks each one for its status every few
seconds; the API proxies any call to a peer at /api/peers/<name>/<path>, so the web UI, CLI and MCP can act on it
exactly as on this machine.
"""
from __future__ import annotations

import base64
import json
import re
from concurrent.futures import ThreadPoolExecutor
import socket
import sys
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

import httpx

def slug_name(raw: str) -> str:
    """Any name people (or host names) come with, as a peer name: "Jobo's MacBook Pro" -> jobos-macbook-pro,
    "Jobos-MacBook-Pro.local" -> jobos-macbook-pro. Empty when nothing usable is left."""
    s = re.sub(r"\.(local|lan|home)$", "", (raw or "").strip().lower())
    s = re.sub(r"['’`]", "", s)
    s = re.sub(r"[^a-z0-9_-]+", "-", s).strip("-_")
    return s[:32].strip("-_")


def normalize_url(raw: str) -> tuple[str, str | None]:
    """How people type a peer's address, made into http(s)://host:port; and the token if they pasted a dashboard link.

    "macbook" -> http://macbook:9000; "http:/macbook:9000" or "http:macbook" (a shell, a browser or autocorrect ate a
    slash) -> http://macbook:9000; "http://macbook:9000/?token=abc#overview" -> (http://macbook:9000, "abc").
    """
    u = (raw or "").strip().strip("'\"<>")
    token = None
    m = re.search(r"[?&]token=([^&#\s]+)", u)
    if m:
        token = m.group(1)
    u = re.sub(r"^(https?):/*", r"\1://", u, flags=re.I)          # http:/x, http:x, http:///x
    if not re.match(r"^https?://", u, re.I):
        u = "http://" + u.lstrip("/")
    scheme, rest = u.split("://", 1)
    host = re.split(r"[/?#]", rest, maxsplit=1)[0]                 # drop any path, query or fragment
    if not host:
        raise ValueError("give the machine's address, e.g. macbook:9000 or http://100.101.102.103:9000")
    if not re.search(r":\d+$", host) and not host.endswith("]"):
        host += ":9000"
    return f"{scheme.lower()}://{host}", token


PAIR = "hangar-pair:"


def make_code(name: str, urls: list[str], token: str) -> str:
    """A pairing code: one line with a machine's name, every address it may be reached at, and its token."""
    raw = json.dumps({"v": 1, "name": name, "urls": urls, "token": token}, separators=(",", ":"))
    return PAIR + base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def parse_code(text: str | None) -> dict | None:
    """The pairing code in text (it may be pasted with other words around it); None when there is none."""
    m = re.search(re.escape(PAIR) + r"([A-Za-z0-9_-]+)", text or "")
    if not m:
        return None
    try:
        b = m.group(1)
        d = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
        return d if isinstance(d, dict) and d.get("urls") and d.get("token") else None
    except (ValueError, TypeError):
        return None


def machine_name(cfg: dict) -> str:
    return slug_name(str(cfg["manager"].get("name") or socket.gethostname())) or "hangar"


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
        p = self.all().get(name) or self.all().get(slug_name(name))
        if not p:
            raise KeyError(f"no peer called {name}")
        return p

    def add(self, name: str | None, url: str | None, token: str | None, check: bool = True, code: str | None = None) -> dict:
        """Add a peer from a pairing code (its addresses are tried in order, the first that answers is kept) or from an
        address and token. Any name is tidied into a peer name (none: the machine's own name, as it reports it)."""
        pair = parse_code(code) or parse_code(url) or parse_code(token)
        if pair:
            candidates = [normalize_url(u)[0] for u in pair["urls"]]
            token = pair["token"]
            name = name or pair.get("name")
        else:
            if not (url or "").strip():
                raise ValueError("paste the other machine's pairing code (Overview > Machines > Share this machine there), "
                                 "or give its address and token")
            u, pasted = normalize_url(url)
            candidates = [u]
            token = (token or "").strip() or pasted or ""
        if not token:
            raise ValueError("the machine's token is needed (data/token.txt there, or paste its pairing code)")
        url, info = self.probe_any(candidates, token) if check else (candidates[0], None)
        name = slug_name(name or "") or slug_name(((info or {}).get("machine") or {}).get("name", ""))
        if not name:
            raise ValueError("give the machine a name, e.g. macbook")
        if name == machine_name(self.cfg):
            raise ValueError(f"{name} is this machine's own name; give the other machine a different one")
        if name in (self.cfg.get("peers") or {}):
            raise ValueError(f"{name} is set in config.yaml; change it there")
        saved = self._saved()
        saved[name] = {"url": url, "token": token}
        self.file.write_text(json.dumps(saved, indent=1), encoding="utf-8")
        self.poll_one(name)
        return {"name": name, "url": url, "machine": (info or {}).get("machine")}

    def remove(self, name: str):
        saved = self._saved()
        name = name if name in saved else slug_name(name)
        if name not in saved:
            raise KeyError(f"no UI-managed peer called {name}" + (" (it is in config.yaml)" if name in self.all() else ""))
        del saved[name]
        self.file.write_text(json.dumps(saved, indent=1), encoding="utf-8")
        with self.lock:
            self.state.pop(name, None)

    @classmethod
    def probe_any(cls, urls: list[str], token: str) -> tuple[str, dict]:
        """The first address (in the given order) where the peer answers with that token, tried all at once."""
        if len(urls) == 1:
            return urls[0], cls.probe(urls[0], token)
        # all at once, with one deadline: a Windows PC name can take 15 s+ just to fail its lookup, and the HTTP
        # timeout does not cover that. The earliest address in the list that has answered by then wins.
        ex = ThreadPoolExecutor(max_workers=min(8, len(urls)))
        futures = [ex.submit(cls.probe, u, token, 5) for u in urls]
        deadline, errors = time.time() + 6, []
        try:
            for u, f in zip(urls, futures):
                try:
                    return u, f.result(timeout=max(0.0, deadline - time.time()))
                except ValueError as e:
                    errors.append(f"{u}: {str(e).split('. ')[0]}")
                except (FutureTimeout, TimeoutError):
                    errors.append(f"{u}: no answer within 6 s")
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        refused = [e for e in errors if "refused the token" in e]
        if refused:
            raise ValueError("the machine answered but refused the token; copy a fresh pairing code from it")
        raise ValueError("none of its addresses answered from here (" + "; ".join(errors) + "). Is Hangar running there, "
                         "listening on every interface (manager.host: 0.0.0.0), and does its firewall let Python accept "
                         "connections (macOS: System Settings > Network > Firewall; SentinelOne can block it too)? Being "
                         "on the same Tailscale network as it is the surest way.")

    @staticmethod
    def probe(url: str, token: str, timeout: float = 8) -> dict:
        """Check a peer answers with that token; its status, or a ValueError saying what is wrong."""
        try:
            r = httpx.get(url + "/api/status", headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
        except httpx.HTTPError as e:
            raise ValueError(f"cannot reach {url} ({type(e).__name__}). Is Hangar running there, and does it listen on "
                             "every interface (manager.host: 0.0.0.0)? Its firewall must let Python accept connections "
                             "(macOS: System Settings > Network > Firewall; endpoint tools like SentinelOne can block "
                             "it too). Open the address in a browser from this machine to check.") from e
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
            st = run.get("stats") or {}
            w = st.get("window") or {}
            g = (st.get("live") or {}).get("gen_tps") or (w.get("1m") or {}).get("gen_tps") or (w.get("5m") or {}).get("gen_tps")
            running.append({"engine": e["name"], "profile": run["profile"], "state": run["state"], "port": e["port"],
                            "gen_tps": g})
    return {"running": running, "engines": len(st.get("engines") or []),
            "vram_used_mib": sum(g.get("vram_used_mib") or 0 for g in gpus),
            "vram_total_mib": sum(g.get("vram_total_mib") or 0 for g in gpus),
            "ram_used_mib": (r.get("ram_total_mib") or 0) - (r.get("ram_available_mib") or 0),
            "ram_total_mib": r.get("ram_total_mib") or 0, "gpu": gpus[0]["name"] if gpus else None,
            "job": (st.get("job") or {}).get("title")}
