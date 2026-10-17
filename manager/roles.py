"""Roles: what a model is *for* - main, helper, ... - and which model fills that role right now.

A role is an ordered list of targets, each a profile on this machine or on a peer. Clients ask the gateway (/v1/* on
this Hangar) for a role by name, e.g. model "helper", and get the first target that is up; when none is, the main
role, then anything running. Targets after the first are backups: with auto_start, a backup is started once the
targets before it have been down for a while - only when it fits next to what runs (nothing is stopped for it, and
the memory budget holds) - and stopped again after it has been idle for a while once the primary is back, if Hangar
started it. Kept in data/settings.json under "roles".
"""
from __future__ import annotations

import json
import re
import threading
import time

import httpx

DOWN_GRACE_S = 30            # a primary must be down this long before a backup is started (sleep/wake flapping)
WAKE_WAIT_S = 90             # how long a request with nothing else to answer it waits for a woken machine
IDLE_STOP_S = 600            # a backup Hangar started is stopped after this long unused, once the primary is back
ROLE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class Roles:
    def __init__(self, mgr, peers):
        self.mgr, self.peers = mgr, peers
        self.lock = threading.Lock()
        self.down_since: dict[str, float] = {}        # role -> when its primary went down
        self.started: dict[tuple, float] = {}         # (machine, profile) -> when Hangar started it as a backup
        self.last_used: dict[tuple, float] = {}       # (machine, profile) -> last request routed to it
        self.inflight: dict[tuple, int] = {}          # (machine, profile) -> requests this gateway has open there
        self.stop_flag = threading.Event()

    # ---- the definitions
    def _settings(self) -> dict:
        return self.mgr._settings()

    def all(self) -> dict[str, dict]:
        return self._settings().get("roles") or {}

    def save(self, name: str, targets: list[dict], spill: bool | None = None) -> dict:
        name = (name or "").strip().lower()
        if not ROLE_NAME.match(name):
            raise ValueError("a role name is a lowercase word, e.g. main or helper")
        clean = []
        for i, t in enumerate(targets or []):
            machine = (t.get("machine") or "").strip().lower()
            prof = (t.get("profile") or "").strip()
            if not prof:
                raise ValueError("every target needs a profile")
            if machine and machine not in self.peers.all():
                raise ValueError(f"no machine called {machine} (add it under Machines first)")
            if not machine and prof not in self.mgr.profiles:
                raise ValueError(f"no profile {prof} on this machine")
            clean.append({"machine": machine, "profile": prof, "auto_start": bool(t.get("auto_start")) and i > 0})
        if not clean:
            raise ValueError("a role needs at least one target")
        s = self._settings()
        if spill is None:                                       # not given: keep what the role had
            spill = bool((s.get("roles") or {}).get(name, {}).get("spill"))
        s.setdefault("roles", {})[name] = {"targets": clean, **({"spill": True} if spill else {})}
        self._write(s)
        self.mgr.store.event("roles", f"role {name}: " + " > ".join(_label(t) for t in clean)
                             + ("; a busy target's requests go to a free one" if spill else ""))
        return self.public()

    def delete(self, name: str):
        s = self._settings()
        if name not in (s.get("roles") or {}):
            raise KeyError(f"no role called {name}")
        del s["roles"][name]
        self._write(s)
        self.mgr.store.event("roles", f"removed role {name}")

    def _write(self, s: dict):
        (self.mgr.data_dir / "settings.json").write_text(json.dumps(s, indent=1), encoding="utf-8")

    # ---- what is up
    def target_state(self, t: dict) -> str:
        """ready | starting | stopped (the machine is up, the profile is not running) | offline | unknown"""
        if not t["machine"]:
            for name, inst in self.mgr.instances.items():
                if inst.profile == t["profile"]:
                    return "ready" if inst.state == "ready" else "starting"
            return "stopped" if t["profile"] in self.mgr.profiles else "unknown"
        st = self.peers.state.get(t["machine"]) or {}
        if not st.get("online"):
            return "offline"
        for e in (st.get("status") or {}).get("engines") or []:
            run = e.get("running")
            if run and run.get("profile") == t["profile"]:
                return "ready" if run.get("state") == "ready" else "starting"
        return "stopped"

    def candidates(self, model: str) -> list[dict]:
        """Who should answer a request for this model name, best first. A role gives its targets that are ready, then
        the main role's, then anything running here, then on peers. A profile id (or profile:<id>) gives just it."""
        roles = self.all()
        if model.startswith("profile:") or (model not in roles and model in self.mgr.profiles):
            pid = model.split(":", 1)[1] if model.startswith("profile:") else model
            t = {"machine": "", "profile": pid}
            return [t] if self.target_state(t) == "ready" else []
        if model not in roles and model != "main":
            return []
        out, seen = [], set()

        def add(t):
            k = (t["machine"], t["profile"])
            if k not in seen and self.target_state(t) == "ready":
                seen.add(k)
                out.append({"machine": t["machine"], "profile": t["profile"]})
        own = (roles.get(model) or {}).get("targets", [])
        if (roles.get(model) or {}).get("spill"):       # the role's free targets first (in order), then its busy ones
            own = [t for t in own if not self.busy(t)] + [t for t in own if self.busy(t)]
        for t in own:
            add(t)
        for t in (roles.get("main") or {}).get("targets", []):
            add(t)
        for name, inst in list(self.mgr.instances.items()):        # anything running here
            add({"machine": "", "profile": inst.profile})
        for name, st in self.peers.state.items():                  # then on the peers
            for e in ((st.get("status") or {}).get("engines") or []) if st.get("online") else []:
                if e.get("running"):
                    add({"machine": name, "profile": e["running"]["profile"]})
        return out

    def wake_for(self, model: str) -> list[str]:
        """Wake the asleep machines a request for this role could use (those set to wake on request); their names.
        Nothing waits here: the request is answered by whatever is up, and the next one gets the woken machine."""
        woken = []
        for t in self._chain(model):
            if self.target_state(t) == "ready":
                break                                   # it is answered; machines further down are not needed
            m = t.get("machine")
            if not m or m in woken or (self.peers.state.get(m) or {}).get("online"):
                continue
            p = self.peers.all().get(m) or {}
            if p.get("wake") and p.get("lan"):
                try:
                    if self.peers.wake(m).get("sent"):
                        self.mgr.store.event("wake", f"woke {m} for a {model} request")
                    woken.append(m)
                except (OSError, ValueError, KeyError) as e:
                    self.mgr.store.event("wake", f"could not wake {m}: {e}", level="warn")
        return woken

    def _chain(self, model: str) -> list[dict]:
        """The targets a request for this model may go to, in order: its role's, then main's. None for a profile
        id or an unknown name (candidates() never sends those to a role)."""
        roles = self.all()
        if model.startswith("profile:") or (model not in roles and model in self.mgr.profiles):
            return []
        if model not in roles and model != "main":
            return []
        targets = list((roles.get(model) or {}).get("targets", []))
        if model != "main":
            targets += (roles.get("main") or {}).get("targets", [])
        return targets

    def starting(self, model: str) -> bool:
        """A target for this request is on its way up (woken or started a moment ago): worth waiting for."""
        roles = self.all()
        if model.startswith("profile:") or (model not in roles and model in self.mgr.profiles):
            pid = model.split(":", 1)[1] if model.startswith("profile:") else model
            return self.target_state({"machine": "", "profile": pid}) == "starting"
        if model == "main" and not (roles.get("main") or {}).get("targets"):       # no main role: anything starting
            if any(i.state == "starting" for i in list(self.mgr.instances.values())):
                return True
            return any((e.get("running") or {}).get("state") == "starting"
                       for st in self.peers.state.values() if st.get("online")
                       for e in (st.get("status") or {}).get("engines") or [])
        return any(self.target_state(t) == "starting" for t in self._chain(model))

    def wait_for(self, model: str, machines: list[str], timeout: float = WAKE_WAIT_S) -> list[dict]:
        """For a request nothing can answer yet, after waking machines or while a target starts: wait for one to
        serve it. A woken machine whose target is not running gets it started."""
        deadline, started = time.time() + timeout, set()
        targets = self._chain(model)
        while time.time() < deadline and not self.stop_flag.is_set():
            for m in machines:
                self.peers.poll_one(m)
            cands = self.candidates(model)
            if cands:
                return cands
            for t in targets:                       # the first one that can come up; never a second behind it
                k = (t.get("machine"), t["profile"])
                state = self.target_state(t)
                if k in started or state in ("ready", "starting"):
                    break
                if t.get("machine") in machines and state == "stopped":
                    started.add(k)
                    try:
                        self._start_target(model, t, "woke its machine for a request; starting", backup=False)
                    except Exception as e:  # noqa: BLE001
                        self.mgr.store.event("wake", f"could not start {_label(t)}: {e}", level="warn")
                    break
            self.stop_flag.wait(2)
        return []

    def busy(self, t: dict) -> bool:
        """Working on a request now: one this gateway sent there, or the engine says so (other clients, last poll)."""
        if self.inflight.get((t["machine"], t["profile"])):
            return True
        if not t["machine"]:
            return any(i.profile == t["profile"] and (i.gauges or {}).get("requests_processing")
                       for i in list(self.mgr.instances.values()))
        st = (self.peers.state.get(t["machine"]) or {}).get("status") or {}
        return any(((e.get("running") or {}).get("profile") == t["profile"]
                    and (((e["running"].get("stats") or {}).get("gauges") or {}).get("requests_processing")))
                   for e in st.get("engines") or [])

    def opened(self, t: dict):
        k = (t["machine"], t["profile"])
        with self.lock:
            self.inflight[k] = self.inflight.get(k, 0) + 1

    def closed(self, t: dict):
        k = (t["machine"], t["profile"])
        with self.lock:
            self.inflight[k] = max(0, self.inflight.get(k, 0) - 1)

    def note_used(self, t: dict):
        self.last_used[(t["machine"], t["profile"])] = time.time()

    # ---- backups: start when the primary has been down a while, stop when it is back and the backup is idle
    def start(self):
        threading.Thread(target=self._loop, name="roles", daemon=True).start()

    def shutdown(self):
        self.stop_flag.set()

    def _loop(self):
        while not self.stop_flag.wait(5):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - keep supervising
                self.mgr.store.event("roles", f"backup check failed: {e}", level="warn")

    def tick(self):
        now = time.time()
        for name, role in self.all().items():
            targets = role.get("targets", [])
            if not targets:
                continue
            primary_up = self.target_state(targets[0]) == "ready"
            if primary_up:
                self.down_since.pop(name, None)
            else:
                self.down_since.setdefault(name, now)
            # start a backup: the primary has been down long enough and nothing earlier in the list is up
            if not primary_up and now - self.down_since[name] >= DOWN_GRACE_S:
                for t in targets[1:]:
                    state = self.target_state(t)
                    if state in ("ready", "starting"):
                        break
                    if t.get("auto_start") and state == "stopped" and self._fits(t):
                        self._start_target(name, t)
                        break
            # stop backups Hangar started, once the primary is back and they sat unused
            if primary_up:
                for t in targets[1:]:
                    k = (t["machine"], t["profile"])
                    if k in self.started and now - max(self.started[k], self.last_used.get(k, 0)) >= IDLE_STOP_S:
                        self._stop_target(name, t)

    def _peer(self, machine: str, method: str, path: str, **kw) -> httpx.Response:
        p = self.peers.get(machine)
        return httpx.request(method, f"{p['url']}/api/{path}", headers={"Authorization": f"Bearer {p['token']}"},
                             timeout=30, **kw)

    def _fits(self, t: dict) -> bool:
        """Only when nothing has to stop for it and it fits the memory there (budget included)."""
        try:
            if t["machine"]:
                plan = self._peer(t["machine"], "GET", "plan", params={"profile": t["profile"]}).json()
            else:
                plan = self.mgr.plan(t["profile"])
        except Exception:  # noqa: BLE001
            return False
        return bool(plan.get("ok")) and not plan.get("will_stop")

    def _start_target(self, role: str, t: dict, why: str | None = None, backup: bool = True):
        k = (t["machine"], t["profile"])
        if t["machine"]:
            self._peer(t["machine"], "POST", "start", json={"profile": t["profile"], "update": "skip"})
        else:
            self.mgr.start(t["profile"], False, "skip")
        if backup:                                  # only backups are stopped again when idle
            self.started[k] = time.time()
        self.mgr.store.event("roles", f"{role}: {why or f'its primary has been down {DOWN_GRACE_S}+ s; starting the backup'} {_label(t)}")

    def _stop_target(self, role: str, t: dict):
        k = (t["machine"], t["profile"])
        engine = None
        if t["machine"]:
            st = (self.peers.state.get(t["machine"]) or {}).get("status") or {}
            engine = next((e["name"] for e in st.get("engines") or [] if (e.get("running") or {}).get("profile") == t["profile"]), None)
            if engine:
                self._peer(t["machine"], "POST", "stop", json={"engine": engine})
        else:
            engine = next((n for n, i in self.mgr.instances.items() if i.profile == t["profile"]), None)
            if engine:
                self.mgr.stop(engine)
        self.started.pop(k, None)
        if engine:
            self.mgr.store.event("roles", f"{role}: the primary is back and the backup {_label(t)} sat idle; stopped it")

    def context(self, name: str) -> int:
        """The smallest context among a role's targets (and main's, its fallback), for the settings clients get."""
        targets = list((self.all().get(name) or {}).get("targets", []))
        if name != "main":
            targets += (self.all().get("main") or {}).get("targets", [])
        if not targets:
            targets = [{"machine": "", "profile": i.profile} for i in self.mgr.instances.values()]
        sizes = []
        for t in targets:
            try:
                if t["machine"]:
                    ps = self._peer(t["machine"], "GET", "profiles").json()
                    p = next((x for x in ps if x["id"] == t["profile"]), None)
                    ctx = (p or {}).get("settings", {}).get("ctx")
                else:
                    from . import clients
                    ents = clients.model_entries(self.mgr, [t["profile"]], "127.0.0.1")
                    ctx = ents[0]["context"] if ents else None
                if ctx:
                    sizes.append(int(ctx))
            except Exception:  # noqa: BLE001 - an unreachable peer just does not count
                pass
        return min(sizes) if sizes else 32768

    # ---- for the dashboard, the status and the Connect page
    def public(self) -> list[dict]:
        out = []
        roles = self.all()
        if "main" not in roles:
            roles = {"main": {"targets": [], "implicit": True}, **roles}
        for name, role in roles.items():
            serving = (self.candidates(name) or [None])[0]
            rows = []
            for i, t in enumerate(role.get("targets", [])):
                k = (t["machine"], t["profile"])
                rows.append({**t, "state": self.target_state(t), "backup": i > 0, "started_by_hangar": k in self.started,
                             "busy": self.busy(t),
                             "serving": bool(serving) and (serving["machine"], serving["profile"]) == k})
            fallback = serving and not any(r["serving"] for r in rows)
            out.append({"name": name, "targets": rows, "implicit": bool(role.get("implicit")), "spill": bool(role.get("spill")),
                        "serving": serving, "fallback": bool(fallback),
                        "down_for": round(time.time() - self.down_since[name]) if name in self.down_since else None})
        return out

    def badges(self) -> dict[str, list[str]]:
        """profile (this machine) -> the roles it is serving now, for the engine cards."""
        out: dict[str, list[str]] = {}
        for r in self.public():
            s = r["serving"]
            if s and not s["machine"]:
                out.setdefault(s["profile"], []).append(r["name"])
        return out


def _label(t: dict) -> str:
    return f"{t['machine'] or 'this machine'}/{t['profile']}"
