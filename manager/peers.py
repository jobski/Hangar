"""Other Hangars this one can see and control (peers): a Mac running MLX next to a Windows PC with CUDA, say.

A peer is a name, the URL other machines reach it at, and its token. They come from config.yaml (`peers:`, read-only)
and from the dashboard/CLI/MCP (kept in data/peers.json). A background loop asks each one for its status every few
seconds; the API proxies any call to a peer at /api/peers/<name>/<path>, so the web UI, CLI and MCP can act on it
exactly as on this machine.
"""
from __future__ import annotations

import base64
import contextlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import socket
import sys
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

import httpx
import psutil

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
    return {"name": machine_name(cfg), "os": {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform),
            "lan": lan_interfaces()}


_lan_cache: tuple[float, list] = (0.0, [])


def lan_interfaces() -> list[dict]:
    """This machine's LAN interfaces (address, netmask, MAC), so a peer can wake it later with a magic packet."""
    global _lan_cache
    if time.time() - _lan_cache[0] < 60:
        return _lan_cache[1]
    out = []
    try:
        stats = psutil.net_if_stats()
        for name, addrs in psutil.net_if_addrs().items():
            if name in stats and not stats[name].isup:
                continue
            mac = next((a.address for a in addrs if a.family == psutil.AF_LINK), "")
            mac = mac.replace("-", ":").lower()
            if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac) or mac == "00:00:00:00:00:00":
                continue
            for a in addrs:
                if a.family == socket.AF_INET and a.netmask and not a.address.startswith(("127.", "169.254.", "100.")):
                    out.append({"ip": a.address, "netmask": a.netmask, "mac": mac})
    except (OSError, ValueError):
        pass
    _lan_cache = (time.time(), out)
    return out


def arp_mac(host: str) -> tuple[str, str] | None:
    """A LAN neighbour's MAC from this machine's ARP table (it is there once we have talked to it): for peers on a
    Hangar too old to report their own."""
    import subprocess
    try:
        ip = socket.gethostbyname(host)
        if sys.platform.startswith("linux"):
            for line in Path("/proc/net/arp").read_text().splitlines()[1:]:
                f = line.split()
                if len(f) >= 4 and f[0] == ip and f[2] != "0x0":
                    mac = f[3].lower()
                    break
            else:
                return None
        else:
            out = subprocess.run(["arp", "-a" if sys.platform == "win32" else "-n", ip], capture_output=True, text=True,
                                 timeout=3, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            m = re.search(r"([0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2}", out)
            if not m:
                return None
            mac = ":".join(x.zfill(2) for x in re.split("[:-]", m.group(0).lower()))
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        return None
    ok = re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac) and mac not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff")
    return (ip, mac) if ok else None


def magic_packet(mac: str) -> bytes:
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16


def send_wake(lan: list[dict], prefer_ip: str | None = None) -> list[str]:
    """Wake-on-LAN: a magic packet for each of a machine's MACs, to its subnet's broadcast and 255.255.255.255."""
    entries = [e for e in lan if e["ip"] == prefer_ip] or lan
    # this machine's own subnets too: a MAC learned from ARP has no netmask, and the packet must reach that subnet
    mine = set()
    for e in lan_interfaces():
        with contextlib.suppress(ValueError):
            mine.add(str(ipaddress.ip_network(f"{e['ip']}/{e['netmask']}", strict=False).broadcast_address))
    sent = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for e in entries:
            pkt = magic_packet(e["mac"])
            targets = {"255.255.255.255", *mine}
            with contextlib.suppress(ValueError, TypeError):
                targets.add(str(ipaddress.ip_network(f"{e['ip']}/{e['netmask']}", strict=False).broadcast_address))
            for t in targets:
                for port in (9, 7):
                    with contextlib.suppress(OSError):
                        s.sendto(pkt, (t, port))
            sent.append(e["mac"])
    return sent


class Peers:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.file: Path = cfg["manager"]["data_dir"] / "peers.json"
        from .config import private_file
        private_file(self.file)
        self.interval = max(3, int(cfg["resources"].get("sample_interval_s", 5)))
        self.lock = threading.Lock()
        self.file_lock = threading.Lock()                # peers.json is read-modified-written from several threads
        self.state: dict[str, dict] = {}
        self.woken: dict[str, float] = {}                # name -> when a magic packet last went to it
        self.callers: dict[str, tuple[float, str]] = {}  # address -> (when, device name)
        self.arp_tried: dict[str, float] = {}            # name -> when its MAC was last looked for in the ARP table
        self.stop_flag = threading.Event()

    # ---- the list
    def _saved(self) -> dict:
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def all(self) -> dict[str, dict]:
        saved = self._saved()
        out = {n: {**p, "source": "managed"} for n, p in saved.items()}
        for n, p in (self.cfg.get("peers") or {}).items():
            learned = saved.get(f"~{str(n).lower()}") or {}      # what was learned about a config peer
            out[str(n).lower()] = {"url": p["url"], "token": p.get("token", ""), "source": "config",
                                   "lan": learned.get("lan") or [],
                                   "wake": bool(learned["wake"] if "wake" in learned else p.get("wake"))}
        out = {n: p for n, p in out.items() if not n.startswith("~")}
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
        with self.file_lock:
            saved = self._saved()
            saved[name] = {**{k: v for k, v in (saved.get(name) or {}).items() if k in ("lan", "wake")}, "url": url, "token": token}
            self._write(saved)
        self.poll_one(name)
        return {"name": name, "url": url, "machine": (info or {}).get("machine")}

    def remove(self, name: str):
        with self.file_lock:
            saved = self._saved()
            name = name if name in saved else slug_name(name)
            if name not in saved or name.startswith("~"):
                raise KeyError(f"no UI-managed peer called {name}" + (" (it is in config.yaml)" if name in self.all() else ""))
            del saved[name]
            self._write(saved)
        with self.lock:
            self.state.pop(name, None)

    def _write(self, saved: dict):
        from .config import write_json
        write_json(self.file, saved)

    def _remember(self, name: str, **kw):
        """Keep something learned about a peer (its LAN interfaces, wake setting) in peers.json; a config.yaml peer's
        goes under ~name."""
        with self.file_lock:
            saved = self._saved()
            key = name if name in saved else f"~{name}"
            cur = saved.get(key) or {}
            if all(cur.get(k) == v for k, v in kw.items()):
                return
            saved[key] = {**cur, **kw}
            self._write(saved)

    # ---- who sent a request: a device name for an address
    def caller_name(self, ip: str | None) -> str:
        """Who sent a request: its DNS host name (the tailnet's MagicDNS name, else reverse DNS, e.g. the home DNS
        server's), else the peer name it has here, else the address. Cached for 10 minutes."""
        if not ip:
            return ""
        if ip in ("127.0.0.1", "::1", "localhost"):
            return machine_name(self.cfg)
        hit = self.callers.get(ip)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        from .clients import tailscale_status
        name = ""
        st = tailscale_status()
        for node in [st.get("Self") or {}, *(st.get("Peer") or {}).values()]:
            if ip in (node.get("TailscaleIPs") or []):
                name = (node.get("DNSName") or "").split(".")[0] or node.get("HostName") or ""
                break
        if not name:
            with contextlib.suppress(OSError, UnicodeError):
                host = socket.gethostbyaddr(ip)[0]
                name = "" if host == ip else host.split(".")[0]
        if not name:
            for n, p in self.all().items():
                if p["url"].split("://", 1)[1].rsplit(":", 1)[0].strip("[]") == ip or ip in [e.get("ip") for e in p.get("lan") or []]:
                    name = n
                    break
        name = (name or ip).lower()[:40]
        self.callers[ip] = (time.time(), name)
        return name

    # ---- wake-on-LAN
    def set_wake(self, name: str, on: bool):
        p = self.get(name)
        if on and not p.get("lan"):
            raise ValueError(f"{name}'s network card is not known yet: it is learned while {name} is online (and on a "
                             "Hangar new enough to report it)")
        self._remember(slug_name(name) if name not in self.all() else name, wake=bool(on))

    def wake(self, name: str, force: bool = False) -> dict:
        """Send a magic packet to a peer (at most every 30 s unless forced). It must be on this machine's network: a
        packet does not cross a router or a VPN."""
        p = self.get(name)
        if not p.get("lan"):
            raise ValueError(f"{name}'s network card is not known yet (it is learned while {name} is online)")
        if not force and time.time() - self.woken.get(name, 0) < 30:
            return {"machine": name, "sent": [], "skipped": "sent in the last 30 s"}
        host = p["url"].split("://", 1)[1].rsplit(":", 1)[0].strip("[]")
        sent = send_wake(p["lan"], host)
        self.woken[name] = time.time()
        return {"machine": name, "sent": sent}

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
        # each machine on its own clock: one that is asleep (a 4 s timeout) does not hold up the others' status
        started: dict[str, float] = {}
        running: set[str] = set()
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="peer-poll") as ex:
            while not self.stop_flag.is_set():
                for name in list(self.all()):
                    if name not in running and time.time() - started.get(name, 0) >= self.interval:
                        started[name] = time.time()
                        running.add(name)
                        ex.submit(self._poll_safe, name, running)
                self.stop_flag.wait(0.5)

    def _poll_safe(self, name: str, running: set):
        try:
            self.poll_one(name)
        except Exception:  # noqa: BLE001 - one bad answer must not stop the watching
            pass
        finally:
            running.discard(name)

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
        lan = ((st or {}).get("machine") or {}).get("lan")
        # a Hangar too old to report its network cards: look for the MAC in the ARP table (now and then)
        if (st and "lan" not in (st.get("machine") or {}) and not p.get("lan")
                and time.time() - self.arp_tried.get(name, 0) > 300):
            self.arp_tried[name] = time.time()
            host = p["url"].split("://", 1)[1].rsplit(":", 1)[0].strip("[]")
            found = arp_mac(host)
            lan = [{"ip": found[0], "netmask": None, "mac": found[1], "from": "arp"}] if found else None
        if lan and lan != p.get("lan"):
            self._remember(name, lan=lan)
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
            wake = {"known": bool(p.get("lan")), "auto": bool(p.get("wake")), "sent": self.woken.get(name)}
            out.append({"name": name, "url": p["url"], "source": p["source"], **s, "wake": wake,
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
            rs = run.get("stats") or {}
            w = rs.get("window") or {}
            g = (rs.get("live") or {}).get("gen_tps") or (w.get("1m") or {}).get("gen_tps") or (w.get("5m") or {}).get("gen_tps")
            running.append({"engine": e["name"], "profile": run["profile"], "state": run["state"], "port": e["port"],
                            "gen_tps": g})
    loads = [g["util_percent"] for g in gpus if g.get("util_percent") is not None]
    return {"running": running, "engines": len(st.get("engines") or []),
            "gen_tps": sum(x["gen_tps"] or 0 for x in running),
            "vram_used_mib": sum(g.get("vram_used_mib") or 0 for g in gpus),
            "vram_total_mib": sum(g.get("vram_total_mib") or 0 for g in gpus),
            "unified": any(g.get("unified") for g in gpus),
            "gpu_util_percent": sum(loads) / len(loads) if loads else None,
            "ram_used_mib": (r.get("ram_total_mib") or 0) - (r.get("ram_available_mib") or 0),
            "ram_total_mib": r.get("ram_total_mib") or 0,
            "cpu_percent": r.get("cpu_percent"), "cpu_count": r.get("cpu_count"),
            "gpu": gpus[0]["name"] if gpus else None, "gpu_count": len(gpus), "job": (st.get("job") or {}).get("title"),
            "gpu_list": [{"name": g.get("name"), "vram_used_mib": g.get("vram_used_mib") or 0, "vram_total_mib":
                          g.get("vram_total_mib") or 0, "util_percent": g.get("util_percent")} for g in gpus]}


def fleet(summaries: list[dict]) -> dict:
    """Every online machine added up: tokens per second out, memory, and load. CPU and GPU load are averaged, CPU
    weighted by thread count (32 busy threads on one machine and 14 idle on another is ~70%, not 50%). On Apple Silicon
    the GPU memory is part of the RAM, so the two totals overlap there. A machine with no engines (a hub) runs no
    models, so it is left out."""
    s = [x for x in summaries if x and x.get("engines", 1)]
    add = lambda k: sum(x.get(k) or 0 for x in s)  # noqa: E731
    cpus = [(x["cpu_percent"], x.get("cpu_count") or 1) for x in s if x.get("cpu_percent") is not None]
    loads = [(x["gpu_util_percent"], x.get("vram_total_mib") or 1) for x in s if x.get("gpu_util_percent") is not None]
    avg = lambda xs: sum(v * w for v, w in xs) / sum(w for _, w in xs) if xs else None  # noqa: E731
    return {"machines": len(s), "running": sum(len(x.get("running") or []) for x in s), "gen_tps": add("gen_tps"),
            "vram_used_mib": add("vram_used_mib"), "vram_total_mib": add("vram_total_mib"),
            "unified": any(x.get("unified") for x in s), "gpus": sum(x.get("gpu_count", 1 if x.get("gpu") else 0) for x in s),
            "gpu_util_percent": avg(loads), "ram_used_mib": add("ram_used_mib"), "ram_total_mib": add("ram_total_mib"),
            "cpu_percent": avg(cpus), "cpu_count": add("cpu_count")}
