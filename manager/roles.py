"""Roles: what a model is *for* - worker, helper, ... - and which model fills that role right now.

A role is an ordered list of targets, each a profile on this machine or on a peer. Clients ask the gateway (/v1/* on
this Hangar) for a role by name, e.g. model "helper", and get the first target that is up; when none is, the worker
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

from .peers import engine_health, engine_reachable

DOWN_GRACE_S = 30            # a primary must be down this long before a backup is started (sleep/wake flapping)
WAKE_WAIT_S = 90             # how long a request with nothing else to answer it waits for a woken machine
IDLE_STOP_S = 600            # a backup Hangar started is stopped after this long unused, once the primary is back
ROLE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def canonical_role(name: str) -> str:
    return "worker" if name == "main" else name


def _definitions(settings: dict) -> dict:
    roles = dict(settings.get("roles") or {})
    legacy = roles.pop("main", None)
    if legacy is not None and "worker" not in roles:
        roles["worker"] = legacy
    return roles


class Roles:
    def __init__(self, mgr, peers):
        self.mgr, self.peers = mgr, peers
        self.lock = threading.RLock()
        self.down_since: dict[str, float] = {}        # role -> when its primary went down
        self.started: dict[tuple, float] = {}         # (machine, profile) -> when Hangar started it as a backup
        self.last_used: dict[tuple, float] = {}       # (machine, profile) -> last request routed to it
        self.inflight: dict[tuple, int] = {}          # (machine, profile) -> requests this gateway has open there
        self.affinity: dict[tuple, tuple] = {}        # (role, client, caller) -> the target that served it last
        self.ctx_cache: dict[tuple, tuple] = {}       # (machine, profile) -> (when, context size or None)
        self.pending: dict[tuple, dict] = {}
        self.warnings: dict[str, float] = {}
        self.client = httpx.Client(timeout=5)
        self.stop_flag = threading.Event()

    # ---- the definitions
    def _settings(self) -> dict:
        return self.mgr._settings()

    def migrate_default_role(self):
        with self.mgr.settings_lock:
            settings = self._settings()
            before = json.dumps(settings, sort_keys=True)
            roles = settings.get("roles") or {}
            legacy = roles.pop("main", None)
            if legacy is not None:
                if "worker" not in roles:
                    roles["worker"] = legacy
                else:
                    targets = roles["worker"].setdefault("targets", [])
                    for target in legacy.get("targets", []):
                        if not any((t.get("machine", ""), t["profile"]) ==
                                   (target.get("machine", ""), target["profile"]) for t in targets):
                            targets.append(target)
                settings["roles"] = roles
            if "main_follows_running" in settings:
                settings.setdefault("worker_follows_running", settings.pop("main_follows_running"))
            if "switching_roles" in settings:
                settings["switching_roles"] = {k: canonical_role(v) for k, v in settings["switching_roles"].items()}
            if json.dumps(settings, sort_keys=True) != before:
                self._write(settings)

    def follows_running(self) -> bool:
        settings = self._settings()
        return settings.get("worker_follows_running", settings.get("main_follows_running", True)) is not False

    def set_follows_running(self, enabled: bool) -> dict:
        with self.mgr.settings_lock:
            settings = self._settings()
            settings["worker_follows_running"] = enabled
            self._write(settings)
        self.mgr.store.event("roles", "worker follows the running model" if enabled else "worker uses saved profile targets")
        return {"enabled": enabled}

    def _current_target(self, machine: str = "") -> dict | None:
        if not machine:
            with getattr(self.mgr, "lock", self.lock):
                running = [{"profile": inst.profile, "state": inst.state, "started": getattr(inst, "started", 0)}
                           for inst in self.mgr.instances.values() if inst.state in ("ready", "starting")]
        else:
            peer = self.peers.state.get(machine) or {}
            status = peer.get("status") or {}
            if status.get("worker_follows_running", status.get("main_follows_running")) is False:
                return None
            running = [engine["running"] for engine in status.get("engines") or []
                       if (engine.get("running") or {}).get("state") in ("ready", "starting")
                       and (peer.get("online") or engine_reachable(peer, engine["running"]["profile"]))]
        if not running:
            return None
        current = max(running, key=lambda inst: (inst.get("state") == "ready", inst.get("started") or 0))
        return {"machine": machine, "profile": current["profile"], "auto_start": False}

    def all(self) -> dict[str, dict]:
        settings = self._settings()
        roles = _definitions(settings)
        if not self.follows_running():
            return roles
        worker = roles.get("worker") or {"targets": [], "implicit": True}
        targets = []
        saved = worker.get("targets", [])
        if not saved:
            current = self._current_target()
            if current:
                targets.append(current)
        for target in saved:
            resolved = self._current_target(target.get("machine", ""))
            target = {**target, "profile": resolved["profile"]} if resolved else dict(target)
            if not any((t["machine"], t["profile"]) == (target["machine"], target["profile"]) for t in targets):
                targets.append(target)
        return {**roles, "worker": {**worker, "targets": targets}}

    def save(self, name: str, targets: list[dict], spill: bool | None = None, switch: bool | None = None) -> dict:
        name = canonical_role((name or "").strip().lower())
        if not ROLE_NAME.match(name):
            raise ValueError("a role name is a lowercase word, e.g. worker or helper")
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
        with self.mgr.settings_lock:
            s = self._settings()
            old = (s.get("roles") or {}).get(name, {})
            if spill is None:                                   # not given: keep what the role had
                spill = bool(old.get("spill"))
            if switch is None:
                switch = bool(old.get("switch"))
            s.setdefault("roles", {})[name] = {"targets": clean, **({"spill": True} if spill else {}),
                                               **({"switch": True} if switch else {})}
            self._write(s)
        self.mgr.store.event("roles", f"role {name}: " + " > ".join(_label(t) for t in clean)
                             + ("; a busy target's requests go to a free one" if spill else "")
                             + ("; a request switches its machine to it" if switch else ""))
        return self.public()

    def delete(self, name: str):
        name = canonical_role(name)
        with self.mgr.settings_lock:
            s = self._settings()
            if name not in (s.get("roles") or {}):
                raise KeyError(f"no role called {name}")
            del s["roles"][name]
            self._write(s)
        self.mgr.store.event("roles", f"removed role {name}")

    def _write(self, s: dict):
        self.mgr.write_settings(s)

    # ---- what is up
    def _peer_engine_state(self, st: dict, profile: str) -> str | None:
        """The state a peer's last status reported for this profile, or None when it wasn't running there."""
        for e in (st.get("status") or {}).get("engines") or []:
            run = e.get("running")
            if run and run.get("profile") == profile:
                return run.get("state")
        return None

    def _target_state(self, t: dict) -> str:
        """ready | starting | stopping (finishing its reply before a switch) | stopped (the machine is up, the profile
        is not running) | offline | unknown"""
        if not t["machine"]:
            for name, inst in list(self.mgr.instances.items()):
                if inst.profile == t["profile"]:
                    return inst.state if inst.state in ("ready", "stopping") else "starting"
            return "stopped" if t["profile"] in self.mgr.profiles else "unknown"
        st = self.peers.state.get(t["machine"]) or {}
        if st.get("online"):
            health = engine_health(st, t["profile"])
            if health is not None and not health["online"]:
                return "offline"
            state = self._peer_engine_state(st, t["profile"])
            if state is None:
                return "stopped"
            return state if state in ("ready", "stopping") else "starting"
        return "ready" if engine_reachable(st, t["profile"]) else "offline"

    def target_state(self, t: dict) -> str:
        state = self._target_state(t)
        key = (t["machine"], t["profile"])
        with self.lock:
            if state == "ready":
                self.pending.pop(key, None)
            elif key in self.pending and state in ("stopped", "unknown"):
                return "starting"
        return state

    def routable(self, t: dict) -> bool:
        """Ready through its manager or a directly verified engine, without an offline time limit."""
        return self.target_state(t) == "ready"

    def candidates(self, model: str, need: int = 0, who: tuple | None = None) -> list[dict]:
        """Who should answer a request for this model name, best first. A role gives its targets that are ready, then
        the worker role's, then anything running here, then on peers. A profile id (or profile:<id>) gives just it.
        need: the request's rough size in tokens; who: (client, caller), to keep a conversation where it is cached."""
        model = canonical_role(model)
        roles = self.all()
        if model.startswith("profile:") or (model not in roles and model in self.mgr.profiles):
            pid = model.split(":", 1)[1] if model.startswith("profile:") else model
            t = {"machine": "", "profile": pid}
            return [t] if self.routable(t) else []
        if model not in roles and model != "worker":
            return []
        out, seen = [], set()

        def add(t):
            k = (t["machine"], t["profile"])
            if k not in seen and self.routable(t):
                seen.add(k)
                out.append({"machine": t["machine"], "profile": t["profile"]})
        own = (roles.get(model) or {}).get("targets", [])
        if (roles.get(model) or {}).get("spill"):
            own = self._spill_order(model, own, need, who)
        for t in own:
            add(t)
        for t in (roles.get("worker") or {}).get("targets", []):
            add(t)
        for name, inst in list(self.mgr.instances.items()):        # anything running here
            add({"machine": "", "profile": inst.profile})
        for name, st in list(self.peers.state.items()):                  # then on the peers
            for e in (st.get("status") or {}).get("engines") or []:
                if e.get("running"):                 # cached details plus direct health cover an offline manager
                    add({"machine": name, "profile": e["running"]["profile"]})
        return sorted(out, key=lambda t: not self.fits(t, need)) if need else out

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
        """The targets a request for this model may go to, in order: its role's, then worker's. None for a profile
        id or an unknown name (candidates() never sends those to a role)."""
        model = canonical_role(model)
        roles = self.all()
        if model.startswith("profile:") or (model not in roles and model in self.mgr.profiles):
            return []
        if model not in roles and model != "worker":
            return []
        targets = list((roles.get(model) or {}).get("targets", []))
        if model != "worker":
            targets += (roles.get("worker") or {}).get("targets", [])
        return targets

    def starting(self, model: str) -> bool:
        """A target for this request is on its way up (woken or started a moment ago): worth waiting for."""
        model = canonical_role(model)
        roles = self.all()
        if model.startswith("profile:") or (model not in roles and model in self.mgr.profiles):
            pid = model.split(":", 1)[1] if model.startswith("profile:") else model
            return self.target_state({"machine": "", "profile": pid}) == "starting"
        if model == "worker" and not (roles.get("worker") or {}).get("targets"):       # no worker role: anything starting
            if any(i.state == "starting" for i in list(self.mgr.instances.values())):
                return True
            return any((e.get("running") or {}).get("state") == "starting"
                       for st in self.peers.state.values() if st.get("online")
                       for e in (st.get("status") or {}).get("engines") or [])
        return any(self.target_state(t) == "starting" for t in self._chain(model))

    def switch_target(self, model: str) -> dict | None:
        """A switching role whose own model is not up: the target its machine should switch to (None: route as
        usual). Never for worker while it follows the running model - it would chase whatever the switch loaded.
        An online peer switches even when its engine probe fails: a model there may be loading."""
        model = canonical_role(model)
        if model == "worker" and self.follows_running():
            return None
        role = _definitions(self._settings()).get(model) or {}
        targets = role.get("targets") or []
        if not role.get("switch") or not targets:
            return None
        t = {"machine": targets[0].get("machine", ""), "profile": targets[0]["profile"]}
        if self.target_state(t) in ("ready", "unknown"):          # up already, or a profile that no longer exists
            return None
        if t["machine"] and not (self.peers.state.get(t["machine"]) or {}).get("online"):
            return None                                           # peer down: the normal route wakes it and falls back
        return t

    def stopping(self, model: str) -> bool:
        """A target for this request is being stopped for a switch: worth a quick retry, not a wait."""
        model = canonical_role(model)
        roles = self.all()
        if model.startswith("profile:") or (model not in roles and model in self.mgr.profiles):
            pid = model.split(":", 1)[1] if model.startswith("profile:") else model
            return self.target_state({"machine": "", "profile": pid}) == "stopping"
        if model == "worker" and not (roles.get("worker") or {}).get("targets"):       # no worker role: anything stopping
            if any(i.state == "stopping" for i in list(self.mgr.instances.values())):
                return True
            return any((e.get("running") or {}).get("state") == "stopping"
                       for st in self.peers.state.values() if st.get("online")
                       for e in (st.get("status") or {}).get("engines") or [])
        return any(self.target_state(t) == "stopping" for t in self._chain(model))

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

    def _spill_order(self, role: str, own: list[dict], need: int, who: tuple | None) -> list[dict]:
        """While the first ready target is busy: a free one that can take the request goes first - the one this
        client used last if it is free (its prompt is cached there), else the first in order. A free target whose
        context is too small for the request is not used for it."""
        ready = [t for t in own if self.routable(t)]
        if not ready or not self.busy(ready[0]):
            return own                                  # the first choice is free: the usual order
        free = [t for t in ready[1:] if not self.busy(t) and self.fits(t, need)]
        last = self.affinity.get((role, *who)) if who else None
        free.sort(key=lambda t: (t["machine"], t["profile"]) != last)       # stable: the client's last one first
        return free + [t for t in own if t not in free]

    def fits(self, t: dict, need: int) -> bool:
        ctx = self.context_of(t)
        return not need or not ctx or need <= ctx

    def context_of(self, t: dict) -> int | None:
        """Routing reads cached peer context; background supervision refreshes older peers."""
        key = (t["machine"], t["profile"])
        if not t["machine"]:
            with self.mgr.lock:
                inst = next((i for i in self.mgr.instances.values() if i.profile == t["profile"]), None)
                if inst and inst.context:
                    return inst.context
            return self.mgr._context(t["profile"])
        st = (self.peers.state.get(t["machine"]) or {}).get("status") or {}
        for engine in st.get("engines") or []:
            running = engine.get("running") or {}
            if running.get("profile") == t["profile"] and running.get("context"):
                return int(running["context"])
        with self.lock:
            cached = self.ctx_cache.get(key)
        return cached[1] if cached and time.time() - cached[0] < 300 else None

    def _refresh_contexts(self):
        for machine, entry in list(self.peers.state.items()):
            if not entry.get("online"):
                continue
            with self.lock:
                fresh = any(k[0] == machine and time.time() - v[0] < 240 for k, v in self.ctx_cache.items())
            if fresh:
                continue
            try:
                profiles = self._peer(machine, "GET", "profiles").json()
                with self.lock:
                    for profile in profiles:
                        ctx = (profile.get("settings") or {}).get("ctx")
                        self.ctx_cache[(machine, profile["id"])] = (time.time(), int(ctx) if ctx else None)
                    while len(self.ctx_cache) > 512:
                        self.ctx_cache.pop(next(iter(self.ctx_cache)))
            except (httpx.HTTPError, KeyError, ValueError, TypeError):
                continue

    def busy(self, t: dict) -> bool:
        """Working on a request now: one this gateway sent there, or the engine says so (other clients, last poll)."""
        if self.inflight.get((t["machine"], t["profile"])):
            return True
        if not t["machine"]:
            return any(i.profile == t["profile"] and (i.gauges or {}).get("requests_processing")
                       for i in list(self.mgr.instances.values()))
        peer = self.peers.state.get(t["machine"]) or {}
        if not peer.get("online"):
            return False
        st = peer.get("status") or {}
        return any(((e.get("running") or {}).get("profile") == t["profile"]
                    and (((e["running"].get("stats") or {}).get("gauges") or {}).get("requests_processing")))
                   for e in st.get("engines") or [])

    def opened(self, t: dict, switching: bool = False):
        """Reserve a request there; switching: its machine will bring the model up and hold the request meanwhile."""
        k = (t["machine"], t["profile"])
        with self.lock:
            if not switching and self._target_state(t) != "ready":
                return False
            self.inflight[k] = self.inflight.get(k, 0) + 1
            return True

    def closed(self, t: dict):
        k = (t["machine"], t["profile"])
        with self.lock:
            count = self.inflight.get(k, 0) - 1
            if count > 0:
                self.inflight[k] = count
            else:
                self.inflight.pop(k, None)
            self.last_used[k] = time.time()

    def note_used(self, t: dict, role: str | None = None, who: tuple | None = None):
        with self.lock:
            self.last_used[(t["machine"], t["profile"])] = time.time()
            if role and who:
                key = (role, *who)
                self.affinity.pop(key, None)
                self.affinity[key] = (t["machine"], t["profile"])
                while len(self.affinity) > 512:
                    self.affinity.pop(next(iter(self.affinity)))

    # ---- backups: start when the primary has been down a while, stop when it is back and the backup is idle
    def start(self):
        self.thread = threading.Thread(target=self._loop, name="roles", daemon=True)
        self.thread.start()

    def shutdown(self):
        self.stop_flag.set()

    def _loop(self):
        try:
            self._supervise()
        finally:
            self.client.close()

    def _supervise(self):
        while not self.stop_flag.wait(5):
            try:
                self._refresh_contexts()
                self._refresh_pending()
                self.tick()
            except Exception as e:  # noqa: BLE001 - keep supervising
                self.mgr.store.event("roles", f"backup check failed: {e}", level="warn")

    def tick(self):
        roles = self.all()
        for name, role in roles.items():
            try:
                self._tick_role(name, role, roles)
            except Exception as exc:
                with self.lock:
                    last = self.warnings.get(name, 0)
                    self.warnings[name] = time.time() if time.time() - last >= 300 else last
                if time.time() - last >= 300:
                    self.mgr.store.event("roles", f"{name}: backup check failed: {exc}", level="warn")

    def _tick_role(self, name: str, role: dict, roles: dict):
        now = time.time()
        targets = role.get("targets", [])
        if not targets:
            return
        primary_up = self.target_state(targets[0]) == "ready"
        with self.lock:
            if primary_up:
                self.down_since.pop(name, None)
            else:
                self.down_since.setdefault(name, now)
            down_since = self.down_since.get(name, now)
        if not primary_up and now - down_since >= DOWN_GRACE_S:
            for target in targets[1:]:
                state = self.target_state(target)
                if state in ("ready", "starting"):
                    break
                if target.get("auto_start") and state == "stopped" and self._fits(target):
                    self._start_target(name, target)
                    break
        if primary_up:
            for target in targets[1:]:
                key = (target["machine"], target["profile"])
                with self.lock:
                    idle = key in self.started and now - max(self.started[key], self.last_used.get(key, 0)) >= IDLE_STOP_S
                needed_elsewhere = any(other != name and self._role_needs(other_role, key)
                                       for other, other_role in roles.items())
                if idle and not needed_elsewhere and not self.busy(target):
                    self._stop_target(name, target)

    def _role_needs(self, role: dict, key: tuple) -> bool:
        if role.get("implicit"):
            return False
        for target in role.get("targets", []):
            if (target["machine"], target["profile"]) == key:
                return True
            if self.target_state(target) == "ready":
                return False
        return False

    def _peer(self, machine: str, method: str, path: str, **kw) -> httpx.Response:
        peer = self.peers.get(machine)
        response = self.client.request(method, f"{peer['url']}/api/{path}",
                                       headers={"Authorization": f"Bearer {peer['token']}"}, **kw)
        response.raise_for_status()
        return response

    def _refresh_pending(self):
        with self.lock:
            pending = list(self.pending.items())
        for key, entry in pending:
            if not entry.get("job"):
                if time.time() - entry["since"] < 60:
                    continue
                state = "unknown"
            else:
                try:
                    if key[0]:
                        job = self._peer(key[0], "GET", f"jobs/{entry['job']}").json()
                        state = job.get("state")
                    else:
                        with self.mgr.lock:
                            job = self.mgr.jobs.get(entry["job"])
                            state = job.state if job else "unknown"
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        continue
                    state = "unknown"
                except KeyError:
                    state = "unknown"
                except (httpx.HTTPError, ValueError):
                    continue
            if state not in ("queued", "running"):
                with self.lock:
                    if self.pending.get(key) is entry:
                        self.pending.pop(key, None)

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
        key = (t["machine"], t["profile"])
        with self.lock:
            if key in self.pending or self._target_state(t) in ("ready", "starting"):
                return False
            entry = {"since": time.time(), "job": None}
            self.pending[key] = entry
        try:
            if t["machine"]:
                job = self._peer(t["machine"], "POST", "start", json={"profile": t["profile"], "update": "skip"}).json()
                entry["job"] = job.get("id")
            else:
                entry["job"] = self.mgr.start(t["profile"], False, "skip").id
        except Exception:
            with self.lock:
                self.pending.pop(key, None)
            raise
        with self.lock:
            if backup:
                self.started[key] = time.time()
        self.mgr.store.event("roles", f"{role}: {why or 'starting backup'} {_label(t)}")
        return True

    def _stop_target(self, role: str, t: dict):
        key = (t["machine"], t["profile"])
        state = self.target_state(t)
        if state == "stopped":
            with self.lock:
                self.started.pop(key, None)
            return
        if state != "ready" or self.busy(t):
            return
        engine = None
        if t["machine"]:
            st = (self.peers.state.get(t["machine"]) or {}).get("status") or {}
            engine = next((e["name"] for e in st.get("engines") or []
                           if (e.get("running") or {}).get("profile") == t["profile"]), None)
            if engine:
                self._peer(t["machine"], "POST", "stop", json={"engine": engine, "idle_profile": t["profile"]})
        else:
            engine = next((n for n, i in list(self.mgr.instances.items()) if i.profile == t["profile"]), None)
            if engine:
                self.mgr.stop_idle(engine, t["profile"])
        if engine:
            self.mgr.store.event("roles", f"{role}: requested stop of idle backup {_label(t)}")

    def context(self, name: str) -> int:
        """The smallest context among a role's targets (and worker's, its fallback), for the settings clients get."""
        name = canonical_role(name)
        targets = list((self.all().get(name) or {}).get("targets", []))
        if name != "worker":
            targets += (self.all().get("worker") or {}).get("targets", [])
        if not targets:
            targets = [{"machine": "", "profile": i.profile} for i in self.mgr.instances.values()]
        sizes = [ctx for target in targets if (ctx := self.context_of(target))]
        return min(sizes) if sizes else 32768

    # ---- for the dashboard, the status and the Connect page
    def public(self) -> list[dict]:
        out = []
        roles = self.all()
        saved_roles = _definitions(self._settings())
        if "worker" not in roles:
            roles = {"worker": {"targets": [], "implicit": True}, **roles}
        roles = {"worker": roles["worker"], **roles}
        for name, role in roles.items():
            serving = (self.candidates(name) or [None])[0]
            rows = []
            for i, t in enumerate(role.get("targets", [])):
                k = (t["machine"], t["profile"])
                rows.append({**t, "state": self.target_state(t), "backup": i > 0, "started_by_hangar": k in self.started,
                             "manager_online": not t["machine"] or bool((self.peers.state.get(t["machine"]) or {}).get("online")),
                             "busy": self.busy(t),
                             "serving": bool(serving) and (serving["machine"], serving["profile"]) == k})
            fallback = serving and not any(r["serving"] for r in rows)
            out.append({"name": name, "targets": rows, "saved_targets": (saved_roles.get(name) or {}).get("targets", []), "implicit": bool(role.get("implicit")), "spill": bool(role.get("spill")),
                        "switch": bool((saved_roles.get(name) or {}).get("switch")),
                        **({"follow_running": self.follows_running()} if name == "worker" else {}),
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
