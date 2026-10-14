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
IDLE_STOP_S = 600            # a backup Hangar started is stopped after this long unused, once the primary is back
ROLE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class Roles:
    def __init__(self, mgr, peers):
        self.mgr, self.peers = mgr, peers
        self.lock = threading.Lock()
        self.down_since: dict[str, float] = {}        # role -> when its primary went down
        self.started: dict[tuple, float] = {}         # (machine, profile) -> when Hangar started it as a backup
        self.last_used: dict[tuple, float] = {}       # (machine, profile) -> last request routed to it
        self.stop_flag = threading.Event()

    # ---- the definitions
    def _settings(self) -> dict:
        return self.mgr._settings()

    def all(self) -> dict[str, dict]:
        return self._settings().get("roles") or {}

    def save(self, name: str, targets: list[dict]) -> dict:
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
        s.setdefault("roles", {})[name] = {"targets": clean}
        self._write(s)
        self.mgr.store.event("roles", f"role {name}: " + " > ".join(_label(t) for t in clean))
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
        for t in (roles.get(model) or {}).get("targets", []):
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

    def _start_target(self, role: str, t: dict):
        k = (t["machine"], t["profile"])
        if t["machine"]:
            self._peer(t["machine"], "POST", "start", json={"profile": t["profile"], "update": "skip"})
        else:
            self.mgr.start(t["profile"], False, "skip")
        self.started[k] = time.time()
        self.mgr.store.event("roles", f"{role}: its primary has been down {DOWN_GRACE_S}+ s; starting the backup {_label(t)}")

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
                             "serving": bool(serving) and (serving["machine"], serving["profile"]) == k})
            fallback = serving and not any(r["serving"] for r in rows)
            out.append({"name": name, "targets": rows, "implicit": bool(role.get("implicit")),
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
