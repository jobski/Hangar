"""The manager core: engines, profiles, running instances, the job queue, resource planning and the stats collector.

Every action that changes something (start, stop, switch, update, rollback) is a Job. Jobs run one at a time on a
single worker thread, so two remote requests can never race each other; each job keeps its own log.
"""
from __future__ import annotations

import collections
import dataclasses
import json
import hashlib
import os
import queue
import re
import shlex
import subprocess
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from . import config as config_mod
from .peers import machine_info
from . import engines as engines_mod
from . import gpumonitor
from . import models as models_mod
from . import resources
from .downloads import Downloader
from .engines import Engine, Profile
from .stats import CounterTracker, Store

CREATE_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
WINDOWS = os.name == "nt"


class Refused(Exception):
    """The action would not fit (or is not allowed) - nothing was changed."""


# --------------------------------------------------------------------------------------------------------- jobs
@dataclass
class Job:
    kind: str
    title: str
    fn: object
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    state: str = "queued"           # queued | running | done | failed | refused
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    log: list[str] = field(default_factory=list)
    log_offset: int = 0
    log_lock: object = field(default_factory=threading.RLock, repr=False)
    result: object = None
    error: str | None = None
    done_event: threading.Event = field(default_factory=threading.Event)

    def write(self, msg: str):
        line = time.strftime("%H:%M:%S ") + msg
        with self.log_lock:
            self.log.append(line)
            if len(self.log) > 2000:
                del self.log[:500]
                self.log_offset += 500

    def public(self, log_lines: int = 50, since: int | None = None) -> dict:
        with self.log_lock:
            start = max(0, since - self.log_offset) if since is not None else max(0, len(self.log) - log_lines)
            if not log_lines:
                start = len(self.log)
            log = self.log[start:start + max(0, log_lines)]
            offset, end = self.log_offset + start, self.log_offset + len(self.log)
        return {"id": self.id, "kind": self.kind, "title": self.title, "state": self.state, "created": self.created,
                "started": self.started, "finished": self.finished, "error": self.error, "result": self.result,
                "log": log, "log_start": offset, "log_next": end}

    def wait(self, timeout: float | None) -> bool:
        return self.done_event.wait(timeout)


# ---------------------------------------------------------------------------------------------------- instances
@dataclass
class Instance:
    engine: str
    profile: str
    pid: int
    create_time: float
    session: int
    log_path: str | None
    ours: bool                       # started by this manager (vs. found running and adopted)
    baseline_vram: float             # GPU memory in use just before it started
    started: float = field(default_factory=time.time)
    ready: float | None = None
    state: str = "starting"          # starting | ready | stopping
    popen: subprocess.Popen | None = None
    tracker: CounterTracker | None = None
    peak_vram: float = 0.0
    vram_measured: bool = False
    vram_isolated: bool = False
    peak_ram: float = 0.0
    ram_mib: float = 0.0
    cpu_percent: float = 0.0
    gauges: dict = field(default_factory=dict)
    custom_profile: Profile | None = None
    context: int | None = None
    context_source: str | None = None
    launched_context: int | None = None
    context_checked: float = 0.0
    startup_pending: bool = False
    last_activity: float = field(default_factory=time.time)   # last time it generated or had a request running
    totals: dict = field(default_factory=lambda: collections.defaultdict(float))   # since this start
    window: collections.deque = field(default_factory=lambda: collections.deque(maxlen=720))  # (t, delta dict)

    def alive(self) -> bool:
        try:
            p = psutil.Process(self.pid)
            return p.is_running() and abs(p.create_time() - self.create_time) < 1 and p.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def persist(self) -> dict:
        return {"engine": self.engine, "profile": self.profile, "pid": self.pid, "create_time": self.create_time,
                "session": self.session, "log_path": self.log_path, "ours": self.ours,
                "baseline_vram": self.baseline_vram, "started": self.started, "ready": self.ready,
                "custom_profile": dataclasses.asdict(self.custom_profile) if self.custom_profile else None,
                "vram_isolated": self.vram_isolated, "context": self.context, "context_source": self.context_source,
                "launched_context": self.launched_context}


# ------------------------------------------------------------------------------------------------------ manager
class Manager:
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or config_mod.load()
        self.data_dir: Path = self.cfg["manager"]["data_dir"]
        self.logs_dir: Path = self.cfg["manager"]["logs_dir"]
        self.store = Store(self.data_dir / "stats.db")
        self._reasoning_cache: dict = {}
        self.lock = threading.RLock()            # guards instances / profiles
        self.settings_lock = threading.RLock()   # data/settings.json is read, changed and written back under it
        self.instances: dict[str, Instance] = {}
        self.engines: dict[str, Engine] = {}
        self.profiles: dict[str, Profile] = {}
        self.jobs: dict[str, Job] = {}
        self.job_order: collections.deque = collections.deque()
        self.queue: queue.Queue[Job] = queue.Queue()
        self.current_job: Job | None = None
        self.snap: dict = resources.snapshot()
        self.history: collections.deque = collections.deque(maxlen=720)     # system samples, ~1 h at 5 s
        self.pending_usage: dict = {}                                         # (minute, engine, profile) -> deltas
        self.stop_flag = threading.Event()
        self._profiles_at = 0.0
        self._estimate_cache = {}
        self.transient: dict[str, Profile] = {}                               # one-off custom runs
        self.crashes: collections.defaultdict = collections.defaultdict(list)  # profile -> crash times
        self.library = models_mod.Library(self.cfg)
        self.downloader = Downloader(self.cfg, self.library.dir, on_done=self._download_done)
        self.load_engines()

    # ---- config
    def load_engines(self):
        engines = {}
        for name, ecfg in (self.cfg.get("engines") or {}).items():
            if ecfg.get("enabled", True) is False:
                continue
            engines[name] = engines_mod.create(name, {**ecfg, "_data_dir": str(self.data_dir)})
        with self.lock:
            for name, inst in self.instances.items():
                if name in engines and name in self.engines:
                    engines[name].active_profile = self.engines[name].active_profile
            self.engines = engines
            self.refresh_profiles(force=True)

    def reload_config(self):
        self.cfg = config_mod.load()
        self.load_engines()
        self.store.event("config", "configuration reloaded")

    def refresh_profiles(self, force: bool = False):
        with self.lock:
            if not force and time.monotonic() - getattr(self, "_profiles_at", 0) < 5:
                return
            self._profiles_at = time.monotonic()
            profiles: dict[str, Profile] = {}
            for e in self.engines.values():
                profiles.update(e.discover_profiles())
            managed = models_mod.load_managed()
            for source, defs in (("managed", managed), ("config", self.cfg.get("profiles") or {})):
                for pid, pcfg in defs.items():
                    eng = self.engines.get((pcfg or {}).get("engine"))
                    if eng is None:
                        continue
                    prof = eng.make_profile(pid, dict(pcfg))
                    prof.source = source
                    profiles[pid] = prof
            profiles.update(self.transient)
            self.profiles = profiles

    def profile(self, pid: str) -> Profile:
        self.refresh_profiles()
        if pid not in self.profiles:
            raise KeyError(f"no profile {pid!r}; profiles: {', '.join(sorted(self.profiles))}")
        return self.profiles[pid]

    def engine(self, name: str) -> Engine:
        if name not in self.engines:
            raise KeyError(f"no engine {name!r}; engines: {', '.join(self.engines)}")
        return self.engines[name]

    # ---- lifecycle
    def start_background(self):
        self.adopt_saved()
        threading.Thread(target=self._worker, name="jobs", daemon=True).start()
        threading.Thread(target=self._collector, name="collector", daemon=True).start()
        threading.Thread(target=self._update_checker, name="update-check", daemon=True).start()
        self.store.event("manager", "manager started")
        threading.Timer(5, self._check_leftovers).start()

    def _check_leftovers(self):
        """Downloads cut off before this start (restart, crash, reboot): say so, and resume them if configured."""
        try:
            left = self.downloader.leftovers()
        except OSError:
            return
        if not left:
            return
        auto = (self.cfg.get("downloads") or {}).get("auto_resume")
        resumed = []
        for lo in left:
            if auto and lo["resumable"]:
                try:
                    self.downloader.resume(lo["folder"])
                    resumed.append(lo["name"])
                except (ValueError, KeyError, OSError) as e:
                    self.store.event("download", f"could not resume {lo['name']}: {e}", level="warn")
        rest = [lo["name"] for lo in left if lo["name"] not in resumed]
        if resumed:
            self.store.event("download", f"resumed unfinished downloads: {', '.join(resumed)}")
        if rest:
            self.store.event("download", f"unfinished downloads: {', '.join(rest)} (Models page: resume or discard)", level="warn")

    def shutdown(self):
        self.stop_flag.set()
        self._flush_usage(force=True)

    # ---- jobs
    def submit(self, kind: str, title: str, fn) -> Job:
        job = Job(kind=kind, title=title, fn=fn)
        with self.lock:
            self.jobs[job.id] = job
            self.job_order.append(job.id)
            self._prune_jobs()
            self.queue.put(job)
        return job

    def _prune_jobs(self):
        completed = [jid for jid in self.job_order if self.jobs[jid].finished is not None]
        for jid in completed[:-100]:
            self.jobs.pop(jid)
            self.job_order.remove(jid)

    def _worker(self):
        while not self.stop_flag.is_set():
            job = self.queue.get()
            self.current_job = job
            job.state, job.started = "running", time.time()
            try:
                job.result = job.fn(job)
                job.state = "done"
            except Refused as e:
                job.state, job.error = "refused", str(e)
                job.write("REFUSED: " + str(e))
                self.store.event(job.kind, f"{job.title}: refused - {e}", level="warn")
            except Exception as e:  # noqa: BLE001 - a job must never kill the worker
                job.state, job.error = "failed", f"{type(e).__name__}: {e}"
                job.write("FAILED: " + job.error)
                for line in traceback.format_exc().splitlines()[-6:]:
                    job.write("  " + line)
                self.store.event(job.kind, f"{job.title}: failed - {job.error}", level="error")
            finally:
                with self.lock:
                    job.finished = time.time()
                    self.current_job = None
                    self._prune_jobs()
                    active = {i.profile for i in self.instances.values()}
                    self.transient = {pid: p for pid, p in self.transient.items() if pid in active}
                job.done_event.set()
                self.queue.task_done()

    def list_jobs(self, limit=20):
        with self.lock:
            jobs = [self.jobs[i] for i in reversed(self.job_order)][:max(0, limit)]
        return [j.public(log_lines=0) for j in jobs]

    # ---- public actions (each returns a Job)
    def start(self, profile_id: str, force: bool = False, update: str = "auto") -> Job:
        p = self.profile(profile_id)
        with self.lock:
            for job in self.jobs.values():
                if getattr(job, "start_profile", None) == p.id and job.state in ("queued", "running"):
                    return job
            job = self.submit("start", f"start {p.id}", lambda job: self._start(job, p, force, update))
            job.start_profile = p.id
            return job

    def stop_idle(self, engine: str, profile: str) -> Job:
        with self.lock:
            expected = self.instances.get(engine)

        def run(job):
            roles = getattr(self, "roles", None)
            with (roles.lock if roles else self.lock), self.lock:
                inst = self.instances.get(engine)
                target = {"machine": "", "profile": profile}
                if inst is not expected or not inst or inst.profile != profile or (roles and roles.busy(target)):
                    job.write("the model changed or is busy; leaving it running")
                    return
                inst.state = "stopping"
            self._stop(job, engine, "idle", expected=expected)
        with self.lock:
            target = (engine, profile)
            for job in self.jobs.values():
                if getattr(job, "idle_target", None) == target and job.state in ("queued", "running"):
                    return job
            job = self.submit("stop", f"stop idle model {profile}", run)
            job.idle_target = target
            return job

    def stop(self, engine: str) -> Job:
        names = list(self.engines) if engine == "all" else [self.engine(engine).name]
        return self.submit("stop", f"stop {engine}", lambda job: [self._stop(job, n, "stopped by request") for n in names])

    def restart(self, engine: str, force: bool = False) -> Job:
        inst = self.instances.get(self.engine(engine).name)
        if not inst:
            raise KeyError(f"{engine} is not running")
        p = self.profile(inst.profile)
        eng = self.engines[p.engine]

        def run(job):
            with self.lock:
                if self.instances.get(eng.name) is not inst:
                    job.write("the running instance changed; skipping this restart")
                    return
            self._stop(job, eng.name, "restart", expected=inst)
            self._wait_vram_released(job)
            self._spawn(job, eng, p)
        return self.submit("restart", f"restart {engine}", run)

    def update(self, engine: str, start_after: bool = True) -> Job:
        e = self.engine(engine)
        if not e.can_update:
            raise KeyError(f"{engine} has no updater")
        return self.submit("update", f"update {engine}", lambda job: self._update(job, e, start_after))

    def rollback(self, engine: str, start_after: bool = True) -> Job:
        e = self.engine(engine)
        if not e.can_rollback:
            raise KeyError(f"{engine} cannot roll back")
        return self.submit("rollback", f"roll back {engine}", lambda job: self._rollback(job, e, start_after))

    def check_updates(self, engine: str | None = None) -> Job:
        names = [engine] if engine else [n for n, e in self.engines.items() if e.can_update]
        return self.submit("check", "check for updates", lambda job: {n: self._check(job, self.engine(n)) for n in names})

    def run_custom(self, profile_id: str, overrides: dict, force: bool = False, update: str = "auto",
                   save_as: str | None = None) -> Job:
        """Start a profile once with changed settings (ctx, args to add, a different mmproj ...), without saving it -
        or save it as a new profile first with save_as."""
        base = self.profile(profile_id)
        eng = self.engines[base.engine]
        cfg = {**base.cfg, "engine": base.engine}
        if eng.plugin == "strata":
            cfg["args"] = list(eng._run_config(base).get("args") or [])
        ov = dict(overrides or {})
        add = [str(a) for a in ov.pop("args_add", []) or []]
        if "args" in ov:
            cfg["args"] = [str(a) for a in ov.pop("args")]
        if add:
            cfg["args"] = list(cfg.get("args", [])) + add
        cfg.update({k: v for k, v in ov.items() if v is not None})
        if save_as:
            self.save_profile(save_as, cfg, overwrite=False)
            return self.start(models_mod.slug(save_as), force, update)
        pid = f"{base.id.split('~custom', 1)[0]}~custom-{uuid.uuid4().hex[:8]}"
        if eng.plugin == "llamacpp" and not cfg.get("alias"):
            cfg["alias"] = pid
        p = eng.make_profile(pid, cfg)
        p.source, p.description = "custom", f"{base.description} (custom run: {json.dumps(overrides)[:120]})"
        return self.submit("start", f"custom run of {base.id}", lambda job: self._start(job, p, force, update))

    # ---- profiles managed from the UI/API (profiles.yaml)
    def save_profile(self, pid: str, data: dict, overwrite: bool = True) -> str:
        pid = models_mod.slug(pid)
        if not data.get("engine") or data["engine"] not in self.engines:
            raise ValueError(f"engine must be one of: {', '.join(self.engines)}")
        if pid in (self.cfg.get("profiles") or {}):
            raise ValueError(f"{pid} is defined in config.yaml; edit it there or use another name")
        with models_mod.edit_managed() as managed:
            if pid in managed and not overwrite:
                raise ValueError(f"profile {pid} already exists")
            if pid in self.profiles and self.profiles[pid].discovered:
                raise ValueError(f"{pid} is a name the {self.profiles[pid].engine} engine already uses")
            managed[pid] = {k: v for k, v in data.items() if v not in (None, "")}
        self.refresh_profiles(force=True)
        self.store.event("profile", f"saved profile {pid}", data["engine"], pid)
        return pid

    def delete_profile(self, pid: str):
        with models_mod.edit_managed() as managed:
            if pid not in managed:
                raise KeyError(f"{pid} is not a UI-managed profile (config.yaml and discovered profiles are edited at "
                               "their source)")
            if any(i.profile == pid for i in list(self.instances.values())):
                raise ValueError(f"{pid} is running; stop it first")
            del managed[pid]
        self.refresh_profiles(force=True)
        self.store.event("profile", f"deleted profile {pid}", None, pid)

    def copy_profile(self, src: str, new_id: str, changes: dict | None = None) -> str:
        p = self.profile(src)
        if p.discovered:
            raise ValueError(f"{src} comes from the {p.engine} engine itself and cannot be copied into a profile")
        return self.save_profile(new_id, {**p.cfg, "engine": p.engine, **(changes or {})}, overwrite=False)

    def delete_model_folder(self, folder: str):
        with self.lock, models_mod.profiles_lock:
            self.refresh_profiles(force=True)
            self.library.delete(folder, self.profiles)
        self.store.event("model", f"deleted {folder}")

    def profile_from_model(self, model: str, name: str | None = None, engine: str = "llamacpp",
                           ctx: int | None = None, mmproj: str | None = None, extra: dict | None = None) -> str:
        pid, prof = models_mod.profile_from_model(model, self.cfg, name, engine, ctx, mmproj, extra,
                                                  limit_mib=self.profile_limit_mib())
        base, n = pid, 2
        while pid in self.profiles:
            pid, n = f"{base}-{n}", n + 1
        return self.save_profile(pid, prof, overwrite=False)

    def _download_done(self, d) -> dict:
        self.store.event("download", f"downloaded {d.repo} into {d.folder}")
        if not d.make_profile:
            return {}
        ggufs = [Path(d.folder) / Path(f["path"]).name for f in d.files if f["path"].lower().endswith(".gguf")]
        mains = sorted(g for g in ggufs if "mmproj" not in g.name.lower()
                       and (not models_mod.SHARD.match(g.name) or "-00001-of-" in g.name))
        if not mains:
            return {}
        pid = self.profile_from_model(str(mains[0]), name=Path(d.folder).name)
        return {"profile": pid}

    # ---- reasoning effort, per profile (kept in data/settings.json; engines that set it live are asked directly)
    def effective(self, p: Profile) -> Profile:
        """The profile as it launches: with the reasoning level chosen for it from the dashboard/CLI/MCP."""
        lvl = (self._settings().get("reasoning") or {}).get(p.id)
        return dataclasses.replace(p, cfg={**p.cfg, "reasoning_effort": lvl}) if lvl else p

    def reasoning(self, engine: str, profile: str | None = None) -> dict:
        eng = self.engine(engine)
        inst = self.instances.get(engine)
        pid = profile or (inst.profile if inst else None)
        if not pid:
            raise ValueError(f"{engine} is not running; name a profile")
        p = self.effective(self.profile(pid))
        levels = eng.reasoning_levels(p)
        running = bool(inst and inst.profile == pid)
        return {"engine": engine, "profile": pid, "supported": bool(levels), "levels": levels or [],
                "level": eng.reasoning_get(p) if levels else None, "running": running,
                "live": type(eng).reasoning_set is not Engine.reasoning_set}

    def set_reasoning(self, engine: str, level: str, profile: str | None = None, restart: bool = False) -> dict:
        info = self.reasoning(engine, profile)
        if not info["supported"]:
            raise ValueError(f"{engine} cannot set a reasoning effort" + (" (this llama.cpp build is too old: update it)"
                                                                          if self.engines[engine].plugin == "llamacpp" else ""))
        if level not in info["levels"]:
            raise ValueError(f"reasoning effort for {engine}: one of {', '.join(info['levels'])}")
        p = self.profile(info["profile"])
        if info["live"]:
            if not info["running"]:
                raise ValueError(f"{engine} sets the reasoning effort while it runs: start {p.id} first")
            self.engines[engine].reasoning_set(self.effective(p), level)
            self.store.event("reasoning", f"{p.id}: reasoning effort {level} (live)", engine, p.id)
            self._reasoning_cache.pop(engine, None)
            return {**self.reasoning(engine, p.id), "applied": "now"}
        with self.settings_lock:
            s = self._settings()
            r = s.setdefault("reasoning", {})
            if level == "default":
                r.pop(p.id, None)
            else:
                r[p.id] = level
            self.write_settings(s)
        self.store.event("reasoning", f"{p.id}: reasoning effort {level}" + (" (restarting)" if restart and info["running"] else ""),
                         engine, p.id)
        out = {**self.reasoning(engine, p.id), "applied": "next start"}
        if info["running"] and restart:
            out.update(applied="restart", job=self.restart(engine).id)
        self._reasoning_cache.pop(engine, None)
        return out

    def _vision(self, engine: str, pid: str) -> bool:
        p = self.profiles.get(pid)
        try:
            return bool(p) and self.engines[engine].has_vision(self.effective(p))
        except Exception:  # noqa: BLE001 - a run config that cannot be read just means no tag
            return False

    def _last_run(self, engine: str) -> dict | None:
        """What a stopped engine ran last, to start it again in one click: the newest run of a profile that still
        exists and is not a one-off custom run; runs that never got ready count only if nothing else did."""
        rows = self.store.recent_sessions(engine)
        for want_ready in (True, False):
            for r in rows:
                p = self.profiles.get(r["profile"])
                if p and p.source != "custom" and (r["ready"] or not want_ready):
                    return {"profile": p.id, "description": p.description, "stopped": r["stopped"],
                            "stop_reason": r["stop_reason"],
                            "ran_seconds": (r["stopped"] - r["started"]) if r["stopped"] and r["started"] else None}
        return None

    def _reasoning_now(self, engine: str) -> dict | None:
        """The running engine's reasoning level for the status, asked at most every 15 s (Strata answers over HTTP)."""
        hit = self._reasoning_cache.get(engine)
        if hit and time.time() - hit[0] < 15:
            return hit[1]
        try:
            r = self.reasoning(engine)
            val = {"level": r["level"], "levels": r["levels"], "live": r["live"]} if r["supported"] else None
        except (ValueError, KeyError):
            val = None
        self._reasoning_cache[engine] = (time.time(), val)
        return val

    # ---- the memory budget: how much of this machine Hangar's engines may use (VRAM + RAM together)
    def write_settings(self, s: dict):
        config_mod.write_json(self.data_dir / "settings.json", s)

    def _settings(self) -> dict:
        try:
            return config_mod.read_json(self.data_dir / "settings.json")
        except (OSError, ValueError):
            return {}

    def budget_mib(self) -> float | None:
        s = self._settings()
        if "memory_budget_mib" in s:
            return s["memory_budget_mib"]
        gb = self.cfg["resources"].get("memory_budget_gb")
        return float(gb) * 1024 if gb else None

    def set_budget(self, gb: float | None) -> dict:
        if gb is not None and not 1 <= gb <= 4096:
            raise ValueError("a memory budget is between 1 and 4096 GB (or none)")
        with self.settings_lock:
            s = self._settings()
            s["memory_budget_mib"] = gb * 1024 if gb else None
            self.write_settings(s)
        self.store.event("budget", f"memory budget {gb:g} GB" if gb else "no memory budget")
        return self.budget_public()

    def footprint(self, vram: float, ram: float) -> float:
        """What a model takes from the machine: VRAM + RAM, or on Apple Silicon (unified memory, where its GPU memory
        is also in its RAM) the larger of the two."""
        return max(vram, ram) if any(g.get("unified") for g in self.snap.get("gpus", [])) else vram + ram

    def budget_public(self) -> dict:
        b = self.budget_mib()
        used = sum(self.footprint(self._instance_vram(i), i.ram_mib or 0) for i in list(self.instances.values()))
        return {"budget_mib": b, "used_mib": used, "unified": any(g.get("unified") for g in self.snap.get("gpus", [])),
                "limit_mib": self.profile_limit_mib()}

    def _instance_vram(self, inst: Instance) -> float:
        if inst.vram_measured:
            return inst.peak_vram
        p = self.profiles.get(inst.profile)
        return self.needs(p)["vram_mib"] if p and p.engine in self.engines else inst.peak_vram

    def profile_limit_mib(self) -> float | None:
        """What a new profile should fit in: the budget, capped on a Mac by what Metal lets the GPU use."""
        b = self.budget_mib()
        gpus = self.snap.get("gpus") or []
        if any(g.get("unified") for g in gpus):
            metal = sum(g["vram_total_mib"] for g in gpus) - self.cfg["resources"]["vram_reserve_mib"]
            return min(b, metal) if b else metal
        return b

    # ---- planning
    def _profile_key(self, p: Profile) -> str:
        profile = self.effective(p)
        files = self.library._references(profile)
        if profile.cfg.get("config"):
            files.append(Path(profile.cfg["config"]))
        expanded = []
        for path in files:
            shard = re.match(r"(.+)-\d{5}-of-(\d{5})\.gguf$", path.name, re.I)
            if shard:
                expanded.extend(path.with_name(f"{shard.group(1)}-{i:05d}-of-{int(shard.group(2)):05d}.gguf")
                                for i in range(1, int(shard.group(2)) + 1))
            else:
                expanded.append(path)
        identities = []
        for path in expanded:
            try:
                st = path.stat()
                identities.append((str(path), st.st_mtime_ns, st.st_size))
            except OSError:
                identities.append((str(path), None, None))
        value = {"profile": profile.cfg, "engine": self.engines[p.engine].cfg, "files": identities}
        return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()

    def needs(self, p: Profile, measurements: dict | None = None) -> dict:
        eng = self.engines[p.engine]
        key = self._profile_key(p)
        cache = self._estimate_cache
        totals = (resources.vram_total(self.snap), self.snap["ram_total_mib"])
        estimate_key = (key, *totals)
        if estimate_key not in cache:
            if len(cache) >= 256:
                cache.clear()
            cache[estimate_key] = eng.estimate(self.effective(p), *totals)
        est = cache[estimate_key]
        learned = self.store.learned(p.id, key) if measurements is None else measurements.get(p.id)
        out = {"vram_mib": est.vram_mib, "ram_mib": est.ram_mib, "greedy_vram": est.greedy_vram,
               "source": "config" if p.cfg.get("requires") else "estimate", "note": est.note}
        if learned and not p.cfg.get("requires"):
            out.update(vram_mib=learned["vram_mib"], ram_mib=learned["ram_mib"], source=f"measured ({learned['runs']} runs)",
                       load_seconds=learned["load_seconds"])
        return out

    def plan(self, profile_id: str, force: bool = False) -> dict:
        """What starting this profile would do, without doing it."""
        p = self.profile(profile_id)
        snap = self.snap if time.time() - self.snap.get("time", 0) < 2 else resources.snapshot(sample_cpu=False)
        need = self.needs(p)
        r = self.cfg["resources"]
        with self.lock:
            others = {n: i for n, i in self.instances.items() if n != p.engine}
            same = self.instances.get(p.engine)
        freed_vram = (self._instance_vram(same) if same else 0.0)
        freed_ram = (same.ram_mib if same else 0.0)
        free_vram = resources.vram_free(snap) + freed_vram
        free_ram = snap["ram_available_mib"] + freed_ram
        stop = [p.engine] if same else []
        warnings = []
        fits = (free_vram >= need["vram_mib"] + r["vram_reserve_mib"] and
                free_ram >= need["ram_mib"] + r["ram_reserve_mib"] and
                not (need["greedy_vram"] and others) and
                not any(self.needs(self.profiles[i.profile])["greedy_vram"] for i in others.values() if i.profile in self.profiles))
        ok, reason = True, ""
        if p.policy == "exclusive":
            stop += list(others)
        elif p.policy == "coexist":
            if not fits:
                if force:
                    stop += list(others)
                    warnings.append("does not fit next to the running engines: stopping them (force)")
                else:
                    ok = False
                    reason = (f"{p.id} needs ~{need['vram_mib']:.0f} MiB VRAM / {need['ram_mib']:.0f} MiB RAM; free: "
                              f"{free_vram:.0f} MiB VRAM / {free_ram:.0f} MiB RAM with {', '.join(others) or 'nothing'} "
                              "running. Stop something first or use force.")
        elif p.policy == "refuse" and others:
            if force:
                stop += list(others)
            else:
                ok, reason = False, f"{p.id} only starts when nothing else runs ({', '.join(others)} running); use force"
        # the memory budget counts what keeps running next to it
        budget = self.budget_mib()
        if ok and budget:
            staying = sum(self.footprint(self._instance_vram(i), i.ram_mib or 0) for n, i in others.items() if n not in stop)
            own = self.footprint(need["vram_mib"], need["ram_mib"])
            total = own + staying
            if total > budget:
                msg = (f"{p.id} needs ~{own / 1024:.1f} GB"
                       + (f" (+{staying / 1024:.1f} GB already running)" if staying else "")
                       + f": over the {budget / 1024:.1f} GB memory budget")
                if force:
                    warnings.append(msg + " (force)")
                else:
                    ok, reason = False, msg + ". Use a smaller context or quant, raise the budget, or force."
        # after stopping, is there enough? (other programs - games, ComfyUI, Ollama - also use the GPU)
        if ok:
            gone = [self.instances[n] for n in stop if n in self.instances]
            v_after = resources.vram_free(snap) + sum(self._instance_vram(i) for i in gone)
            m_after = snap["ram_available_mib"] + sum(i.ram_mib for i in gone)
            if v_after < need["vram_mib"] * 0.8 and not need["greedy_vram"]:
                warnings.append(f"only ~{v_after:.0f} MiB VRAM will be free (needs ~{need['vram_mib']:.0f}): "
                                "another program is using the GPU")
            elif need["greedy_vram"] and v_after < resources.vram_total(snap) * 0.6:
                warnings.append(f"only ~{v_after:.0f} of {resources.vram_total(snap):.0f} MiB VRAM will be free: "
                                "another program is using the GPU, so this will run slower")
            if m_after < need["ram_mib"]:
                warnings.append(f"only ~{m_after:.0f} MiB RAM will be free (needs ~{need['ram_mib']:.0f}): "
                                "expect swapping")
        return {"profile": p.id, "engine": p.engine, "policy": p.policy, "ok": ok, "reason": reason,
                "will_stop": sorted(set(stop)), "needs": need, "budget_mib": budget, "free_vram_mib": resources.vram_free(snap),
                "free_ram_mib": snap["ram_available_mib"], "warnings": warnings}

    # ---- the work
    def _start(self, job: Job, p: Profile, force: bool, update: str):
        if p.source == "custom":
            with self.lock:
                self.transient[p.id] = p
        eng = self.engines[p.engine]
        self.refresh_profiles(force=True)
        plan = self.plan(p.id, force)
        job.write(f"plan: policy {plan['policy']}, needs ~{plan['needs']['vram_mib']:.0f} MiB VRAM / "
                  f"{plan['needs']['ram_mib']:.0f} MiB RAM ({plan['needs']['source']})")
        for warning in plan["warnings"]:
            job.write("warning: " + warning)
        if not plan["ok"]:
            raise Refused(plan["reason"])
        owner = resources.port_owner(eng.port, fresh=True)
        current = self.instances.get(eng.name)
        if owner is not None and (not current or owner.pid != current.pid) and not force:
            raise Refused(f"port {eng.port} is in use outside this manager; stop its owner or use force")
        previous = {n: self.profiles.get(self.instances[n].profile) for n in plan["will_stop"] if n in self.instances}
        try:
            for name in plan["will_stop"]:
                self._stop(job, name, f"making room for {p.id}")
            if previous:
                self._wait_vram_released(job)
                if p.policy == "coexist":
                    actual = self.plan(p.id, force)
                    if not actual["ok"]:
                        raise Refused(actual["reason"])
            if update != "skip" and eng.can_update and self.cfg["updates"]["auto_before_start"]:
                known = self._update_info(eng.name)
                last = known.get("checked", 0)
                if update == "force" or known.get("available") or time.time() - last > self.cfg["updates"]["min_interval_hours"] * 3600:
                    try:
                        self._update(job, eng, start_after=False)
                    except Exception as exc:
                        job.write(f"update failed, starting the installed version: {exc}")
                        self.store.event("update", f"{eng.name}: update before start failed: {exc}", eng.name, level="warn")
            owner = resources.port_owner(eng.port, fresh=True)
            if owner is not None:
                if not force or owner.pid is None:
                    raise Refused(f"port {eng.port} is still in use; no process was stopped")
                _kill_tree(owner.pid)
                time.sleep(2)
            self._spawn(job, eng, p)
        except Exception:
            for name, previous_profile in previous.items():
                if previous_profile is not None and name not in self.instances:
                    try:
                        self._spawn(job, self.engines[name], previous_profile)
                    except Exception as exc:
                        job.write(f"could not restore {previous_profile.id}: {exc}")
            raise
        return {"profile": p.id, "engine": eng.name, "port": eng.port, "url": f"http://<this-pc>:{eng.port}"}

    def _spawn(self, job: Job, eng: Engine, p: Profile):
        launch = eng.launch(self.effective(p))
        launched_context = eng.launch_context(launch.cmd)
        log_path = self.logs_dir / f"{eng.name}.log"
        if log_path.exists() and log_path.stat().st_size > 50 * 2**20:
            log_path.replace(log_path.with_suffix(".old.log"))
        f = open(log_path, "a", encoding="utf-8", errors="replace")
        cmd = _redact_command(launch.cmd)
        shown = subprocess.list2cmdline(cmd) if WINDOWS else shlex.join(cmd)
        f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} start {p.id}\n{shown}\n")
        f.flush()
        with self.lock:
            isolated = not self.instances
            for other in self.instances.values():
                other.vram_isolated = False
        baseline = resources.vram_used()
        job.write(f"starting {p.id} on port {eng.port}")
        # Python engines block-buffer stdout into a file; unbuffered, their log follows live
        env = {**os.environ, "PYTHONUNBUFFERED": "1", **launch.env}
        common = dict(cwd=launch.cwd, env=env, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT)
        try:
            if not WINDOWS:
                # own session: the engine outlives a manager restart, as the breakaway flag gives on Windows
                proc = subprocess.Popen(launch.cmd, start_new_session=True, **common)
            else:
                try:
                    proc = subprocess.Popen(launch.cmd, creationflags=CREATE_FLAGS | BREAKAWAY, **common)
                except OSError:
                    proc = subprocess.Popen(launch.cmd, creationflags=CREATE_FLAGS, **common)
        finally:
            f.close()
        sid = self.store.session_start(eng.name, p.id, config_key=self._profile_key(p))
        inst = Instance(engine=eng.name, profile=p.id, pid=proc.pid, create_time=psutil.Process(proc.pid).create_time(),
                        session=sid, log_path=str(log_path), ours=True, baseline_vram=baseline, popen=proc,
                        tracker=CounterTracker(count_first=True), startup_pending=True,
                        context=launched_context, launched_context=launched_context,
                        context_source="launch" if launched_context else None,
                        custom_profile=p if p.source == "custom" else None)
        with self.lock:
            inst.vram_isolated = isolated and not self.instances
            if p.source == "custom":
                self.transient[p.id] = self.profiles[p.id] = p
            self.instances[eng.name] = inst
            eng.active_profile = p
        self._save_state()
        self.store.event("start", f"starting {p.id}", eng.name, p.id)

        try:
            deadline = time.time() + eng.startup_timeout_s
            last_detail, last_note = "", 0.0
            while time.time() < deadline:
                if proc.poll() is not None:
                    tail = self.tail_log(eng.name, 15)
                    self._forget(eng.name, f"exited during start (code {proc.returncode})", expected=inst)
                    raise RuntimeError(f"{p.id} exited during start (code {proc.returncode}). Log:\n" + "\n".join(tail))
                ok, detail = eng.health(p)
                if ok:
                    inst.ready, inst.state, inst.startup_pending = time.time(), "ready", False
                    self.store.session_ready(sid)
                    self._save_state()
                    job.write(f"{p.id} is ready after {inst.ready - inst.started:.0f} s")
                    self.store.event("ready", f"{p.id} ready after {inst.ready - inst.started:.0f} s", eng.name, p.id)
                    return
                if detail != last_detail or time.time() - last_note > 30:
                    job.write(f"  waiting: {detail}")
                    last_detail, last_note = detail, time.time()
                time.sleep(2)
            self._stop(job, eng.name, "did not get ready in time", expected=inst)
            raise RuntimeError(f"{p.id} was not ready after {eng.startup_timeout_s} s; stopped it. Log:\n"
                               + "\n".join(self.tail_log(eng.name, 15)))
        except Exception:
            self._stop(job, eng.name, "failed during startup", expected=inst)
            raise
        finally:
            inst.startup_pending = False

    def _stop(self, job: Job | None, engine: str, reason: str, expected: Instance | None = None):
        with self.lock:
            inst = self.instances.get(engine)
            if expected is not None and inst is not expected:
                return
            if inst:
                inst.state = "stopping"
        if not inst:
            if job:
                job.write(f"{engine} is not running")
            return
        if job:
            job.write(f"stopping {inst.profile} ({reason})")
        if inst.alive():
            _kill_tree(inst.pid)
        self._forget(engine, reason, expected=inst)

    def _forget(self, engine: str, reason: str, expected: Instance | None = None) -> bool:
        with self.lock:
            if expected is not None and self.instances.get(engine) is not expected:
                return False
            inst = self.instances.pop(engine, None)
            if engine in self.engines:
                self.engines[engine].active_profile = None
        if inst:
            self._flush_usage(force=True)
            self.store.session_stop(inst.session, reason)
            self.store.event("stop", f"{inst.profile}: {reason}", engine, inst.profile)
        self._save_state()
        return inst is not None

    def _wait_vram_released(self, job: Job):
        """The driver hands memory back a moment after a process ends: wait until VRAM use stops falling."""
        timeout = self.cfg["resources"]["vram_release_timeout_s"]
        end, last, steady = time.time() + timeout, None, 0
        while time.time() < end:
            used = resources.vram_used()
            steady = steady + 1 if last is not None and used >= last - 64 else 0
            if steady >= 2:
                break
            last = used
            time.sleep(1.5)
        job.write(f"VRAM free now: {resources.vram_free():.0f} MiB")

    def _update_info(self, engine: str) -> dict:
        try:
            return json.loads(self.store.get(f"update:{engine}", "{}"))
        except ValueError:
            return {}

    def _check(self, job: Job, eng: Engine) -> dict:
        try:
            info = eng.check_update(job.write)
            info["checked"] = time.time()
        except Exception as e:  # noqa: BLE001
            info = {**self._update_info(eng.name), "checked": time.time(), "error": str(e)}
            job.write(f"{eng.name}: update check failed: {e}")
        self.store.put(f"update:{eng.name}", json.dumps(info))
        return info

    def _update(self, job: Job, eng: Engine, start_after: bool):
        info = self._check(job, eng)
        if info.get("error"):
            raise RuntimeError(info["error"])
        if not info.get("available"):
            return info
        running = self.instances.get(eng.name)
        prof = running.profile if running else None
        previous_profile = self.profiles.get(prof)
        if running:
            self._stop(job, eng.name, "updating")
            self._wait_vram_released(job)
        old = eng.version()
        try:
            res = eng.update(job.write)
        except Exception:
            if previous_profile:
                try:
                    self._spawn(job, eng, previous_profile)
                except Exception as exc:
                    job.write(f"could not restore {prof} after update failure: {exc}")
            raise
        res["checked"] = time.time()
        res["available"] = False
        self.store.put(f"update:{eng.name}", json.dumps(res))
        self.store.event("update", f"{eng.name}: {old} -> {eng.version()}", eng.name)
        self.refresh_profiles(force=True)
        if start_after and previous_profile:
            self._spawn(job, eng, previous_profile)
        return res

    def _rollback(self, job: Job, eng: Engine, start_after: bool):
        running = self.instances.get(eng.name)
        prof = running.profile if running else None
        previous_profile = self.profiles.get(prof)
        if running:
            self._stop(job, eng.name, "rolling back")
            self._wait_vram_released(job)
        try:
            res = eng.rollback(job.write)
        except Exception:
            if previous_profile:
                try:
                    self._spawn(job, eng, previous_profile)
                except Exception as exc:
                    job.write(f"could not restore {prof} after rollback failure: {exc}")
            raise
        self.store.event("rollback", f"{eng.name}: now {eng.version()}", eng.name)
        if start_after and previous_profile:
            self._spawn(job, eng, previous_profile)
        return res

    # ---- adoption: engines that were running before the manager (re)started
    def _save_state(self):
        with self.lock:                          # held while writing: the newest snapshot is the one that lands
            data = {n: i.persist() for n, i in self.instances.items()}
            config_mod.write_json(self.data_dir / "state.json", data)

    def adopt_saved(self):
        f = self.data_dir / "state.json"
        keep = set()
        if f.exists():
            try:
                saved = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                saved = {}
            if not isinstance(saved, dict):
                saved = {}
            for name, d in saved.items():
                if name not in self.engines or not isinstance(d, dict) or not {"profile", "pid", "create_time", "session"}.issubset(d):
                    continue
                if (not isinstance(d["profile"], str) or not isinstance(d["pid"], int) or d["pid"] <= 0
                        or not isinstance(d["session"], int) or not isinstance(d["create_time"], (int, float))
                        or any(d.get(k) is not None and not isinstance(d[k], (int, float))
                               for k in ("create_time", "started", "ready", "baseline_vram"))):
                    continue
                try:
                    custom = Profile(**{k: v for k, v in d["custom_profile"].items() if k in Profile.__dataclass_fields__}) if d.get("custom_profile") else None
                    if custom and (custom.id != d["profile"] or custom.engine != name or not isinstance(custom.cfg, dict)):
                        custom = None
                except (TypeError, AttributeError):
                    custom = None
                if d["profile"] not in self.profiles and custom is None:
                    continue
                inst = Instance(engine=name, profile=d["profile"], pid=d["pid"], create_time=d["create_time"],
                                session=d["session"], log_path=d.get("log_path"), ours=d.get("ours", True),
                                baseline_vram=d.get("baseline_vram", 0), started=d.get("started", time.time()),
                                ready=d.get("ready"), state="ready" if d.get("ready") else "starting",
                                tracker=CounterTracker(count_first=False), custom_profile=custom,
                                vram_isolated=d.get("vram_isolated", False),
                                context=d.get("context") if isinstance(d.get("context"), int) and d["context"] > 0 else None,
                                context_source=d.get("context_source") if d.get("context_source") in ("launch", "runtime") else None,
                                launched_context=d.get("launched_context") if isinstance(d.get("launched_context"), int) and d["launched_context"] > 0 else None)
                if inst.alive():
                    if not inst.launched_context:
                        try:
                            process = psutil.Process(inst.pid)
                            try:
                                cwd = process.cwd()
                            except psutil.Error:
                                cwd = None
                            inst.launched_context = self.engines[name].process_context(
                                process.cmdline(), inst.create_time, cwd)
                            if not inst.context:
                                inst.context = inst.launched_context
                                inst.context_source = "launch" if inst.context else None
                        except psutil.Error:
                            pass
                    with self.lock:
                        if custom:
                            self.transient[custom.id] = self.profiles[custom.id] = custom
                        self.instances[name] = inst
                        self.engines[name].active_profile = self.profiles[d["profile"]]
                    keep.add(inst.session)
        self.store.close_dangling_sessions(keep)
        self._discover_external()
        self._save_state()

    def _discover_external(self):
        """An engine's port is in use but not by us: if the command line matches a profile, take it over."""
        for name, eng in self.engines.items():
            if name in self.instances:
                continue
            owner = resources.port_owner(eng.port, fresh=True)
            if owner is None:
                continue
            try:
                cmd = owner.cmdline()
                ctime = owner.create_time()
            except psutil.Error:
                continue
            pid = eng.match_process(cmd, self.profiles)
            if not pid:
                continue
            with self.lock:
                if name in self.instances or self.current_job is not None:
                    continue
                sid = self.store.session_start(name, pid, adopted=True)
                inst = Instance(engine=name, profile=pid, pid=owner.pid, create_time=ctime, session=sid, log_path=None,
                                ours=False, baseline_vram=0.0, started=ctime, state="starting",
                                tracker=CounterTracker(count_first=False))
                for other in self.instances.values():
                    other.vram_isolated = False
                self.instances[name] = inst
                eng.active_profile = self.profiles[pid]
            ok, _ = eng.health(self.profiles[pid])
            if ok:
                inst.ready, inst.state = time.time(), "ready"
                self.store.session_ready(sid)
            try:
                cwd = owner.cwd()
            except psutil.Error:
                cwd = None
            inst.context = inst.launched_context = eng.process_context(cmd, ctime, cwd)
            inst.context_source = "launch" if inst.context else None
            self.store.event("adopt", f"found {pid} already running (pid {owner.pid}); now managed", name, pid)
            self._save_state()

    # ---- collector: resources, crashes, token counters
    def _collector(self):
        interval = self.cfg["resources"]["sample_interval_s"]
        last_discover = last_prune = 0.0
        while not self.stop_flag.wait(interval):
            try:
                self._sample()
                if time.time() - last_prune > 6 * 3600:      # the request log keeps manager.request_log_days
                    self.store.prune_requests(float(self.cfg["manager"].get("request_log_days", 365)))
                    last_prune = time.time()
                if time.time() - last_discover > 30 and (self.current_job is None):
                    self._discover_external()
                    last_discover = time.time()
                self._flush_usage()
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    def _sample(self):
        snap = resources.snapshot()
        self.snap = snap
        used = resources.vram_used(snap)
        point = {"t": snap["time"], "vram_used_mib": used, "ram_used_mib": snap["ram_total_mib"] - snap["ram_available_mib"],
                 "cpu_percent": snap["cpu_percent"],
                 "gpu_util": max((g["util_percent"] or 0 for g in snap["gpus"]), default=0), "gen_tokens": 0.0,
                 "prompt_tokens": 0.0}
        with self.lock:
            insts = list(self.instances.values())
        per_pid = resources.gpu_process_memory() if insts and snap["gpus"] and not any(
            g.get("unified") for g in snap["gpus"]) else None
        for inst in insts:
            if inst.state == "stopping" or inst.startup_pending:
                continue
            if not inst.alive():
                code = inst.popen.poll() if inst.popen else None
                with self.lock:
                    if self._forget(inst.engine, "exited unexpectedly" + (f" (code {code})" if code is not None else ""),
                                    expected=inst):
                        self.store.event("crash", f"{inst.profile} exited unexpectedly", inst.engine, inst.profile, level="error")
                        self._maybe_restart(inst)
                continue
            u = resources.tree_usage(inst.pid)
            inst.ram_mib, inst.cpu_percent = u["ram_mib"], u["cpu_percent"]
            inst.peak_ram = max(inst.peak_ram, inst.ram_mib)
            measured = None
            if per_pid is not None and any(pid in per_pid for pid in u["pids"]):
                measured = sum(per_pid.get(pid, 0.0) for pid in u["pids"])
            elif not snap["gpus"]:
                measured = 0.0
            if measured is not None:
                inst.peak_vram = max(inst.peak_vram, measured)
                inst.vram_measured = True
            if inst.ready:
                self.store.session_peak(inst.session, inst.peak_vram if inst.vram_measured else None, inst.peak_ram)
            eng = self.engines.get(inst.engine)
            if eng is None:
                continue
            if inst.state == "starting" and not (self.current_job and inst.ours):
                ok, _ = eng.health(self.profiles.get(inst.profile))
                if ok:
                    inst.ready, inst.state = time.time(), "ready"
                    self.store.session_ready(inst.session)
            if inst.state == "ready" and time.time() - inst.context_checked >= 60:
                ctx = eng.runtime_context()
                inst.context_checked = time.time()
                if ctx:
                    inst.context, inst.context_source = ctx, "runtime"
            cur = eng.scrape() if inst.state == "ready" else None
            if cur is None:
                continue
            inst.gauges = {k.split(":", 1)[1]: v for k, v in cur.items()
                           if k.endswith(("requests_processing", "requests_deferred", "model_loaded"))}
            d = inst.tracker.delta(cur)
            if d or inst.gauges.get("requests_processing"):
                inst.last_activity = snap["time"]
            self._maybe_idle_stop(inst)
            if d:
                with self.lock:
                    for k, v in d.items():
                        inst.totals[k] += v
                    inst.window.append((snap["time"], d))
                    key = (int(snap["time"] // 60), inst.engine, inst.profile)
                    acc = self.pending_usage.setdefault(key, collections.defaultdict(float))
                    for k, v in d.items():
                        acc[k] += v
                point["gen_tokens"] += d.get("gen_tokens", 0)
                point["prompt_tokens"] += d.get("prompt_tokens", 0)
        self.history.append(point)

    def _setting(self, inst: Instance, key: str, default=None):
        """A per-profile setting, falling back to the engine's config."""
        p = self.profiles.get(inst.profile)
        if p and key in p.cfg:
            return p.cfg[key]
        return (self.cfg["engines"].get(inst.engine) or {}).get(key, default)

    def _maybe_idle_stop(self, inst: Instance):
        minutes = float(self._setting(inst, "idle_stop_minutes", 0) or 0)
        if not minutes or not inst.ready or inst.state != "ready" or self.current_job or not self.queue.empty():
            return
        with self.lock:
            idle = self.instances.get(inst.engine) is inst and time.time() - max(inst.last_activity, inst.ready) > minutes * 60
        if idle:
            self.stop_idle(inst.engine, inst.profile)

    def _maybe_restart(self, inst: Instance):
        if not self._setting(inst, "restart_on_crash", False) or inst.profile not in self.profiles:
            return
        now = time.time()
        recent = [t for t in self.crashes[inst.profile] if now - t < 3600]
        self.crashes[inst.profile] = recent + [now]
        if len(recent) >= 3:
            self.store.event("crash", f"{inst.profile} crashed 3 times within an hour: not restarting it again",
                             inst.engine, inst.profile, level="error")
            return
        p = self.profiles[inst.profile]

        def restart_if_down(job):
            with self.lock:
                if inst.engine in self.instances:
                    job.write(f"{inst.engine} is already running again; skipping the crash restart")
                    return
            return self._start(job, p, False, "skip")
        self.submit("start", f"restart {p.id} after a crash", restart_if_down)

    def _flush_usage(self, force: bool = False):
        with self.lock:
            now_min = int(time.time() // 60)
            for key in list(self.pending_usage):
                if force or key[0] < now_min:
                    d = self.pending_usage.pop(key)
                    self.store.add_usage(key[0], key[1], key[2], d)

    def _update_checker(self):
        hours = self.cfg["updates"]["background_check_hours"]
        if not hours:
            return
        self.stop_flag.wait(60)
        while not self.stop_flag.is_set():
            due = [n for n, e in self.engines.items() if e.can_update and
                   time.time() - self._update_info(n).get("checked", 0) > hours * 3600]
            if due and self.current_job is None and self.queue.empty():
                self.submit("check", "background update check",
                            lambda job, due=due: {n: self._check(job, self.engines[n]) for n in due})
            self.stop_flag.wait(600)

    # ---- reading
    def log_path(self, engine: str) -> Path:
        inst = self.instances.get(engine)
        return Path(inst.log_path) if inst and inst.log_path else self.logs_dir / f"{engine}.log"

    def follow_log(self, engine: str, pos: int | None, ident=None, lines: int = 200) -> tuple[bytes, int, object]:
        """One step of following a log: (new bytes, new position, file identity). pos=None starts with the last lines.
        The file is opened per call, not held, so the rotation at engine start (a rename) still works on Windows.
        A new file (rotated, or the engine moved to another log) is read from its start."""
        path = self.log_path(engine)
        try:
            st = path.stat()
        except OSError:
            return (("\n".join(self.tail_log(engine, lines)) + "\n").encode() if pos is None else b""), 0, None
        cur = (str(path), st.st_dev, st.st_ino)     # not ctime: Windows tunnelling copies it onto a re-created file
        if pos is None:
            return ("\n".join(self.tail_log(engine, lines)) + "\n").encode(), st.st_size, cur
        if cur != ident or st.st_size < pos:
            pos = 0
        if st.st_size == pos:
            return b"", pos, cur
        with open(path, "rb") as f:
            f.seek(pos)
            data = f.read(min(st.st_size - pos, 2**20))
        return data, pos + len(data), cur

    def tail_log(self, engine: str, lines: int = 100) -> list[str]:
        inst = self.instances.get(engine)
        path = self.log_path(engine)
        if not path.exists():
            return [f"(no log yet at {path})" + ("; this engine was started outside the manager" if inst and not inst.ours else "")]
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start, chunks, count = size, [], 0
            while start > 0 and count <= lines and size - start < 256 * 1024:
                length = min(4096, start)
                start -= length
                f.seek(start)
                chunk = f.read(length)
                chunks.append(chunk)
                count += chunk.count(b"\n")
            data = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
        return data.splitlines()[-lines:] if lines > 0 else []

    def live(self, inst: Instance) -> dict:
        now = time.time()
        out = {}
        with self.lock:
            window = list(inst.window)
            totals = dict(inst.totals)
        for label, secs in (("1m", 60), ("5m", 300), ("1h", 3600)):
            agg = collections.defaultdict(float)
            for t, d in window:
                if now - t <= secs:
                    for k, v in d.items():
                        agg[k] += v
            out[label] = {"gen_tokens": agg["gen_tokens"], "prompt_tokens": agg["prompt_tokens"],
                          "requests": agg["requests"],
                          "gen_tps": agg["gen_tokens"] / agg["gen_seconds"] if agg["gen_seconds"] else None,
                          "prompt_tps": agg["prompt_tokens"] / agg["prompt_seconds"] if agg["prompt_seconds"] else None}
        t = collections.defaultdict(float, totals)
        live = None
        if inst.gauges.get("requests_processing"):
            eng = self.engines.get(inst.engine)
            path = self.log_path(inst.engine)
            try:
                fresh = time.time() - path.stat().st_mtime < 5      # progress lines are printed every second
            except OSError:
                fresh = False
            if eng and fresh:
                live = eng.progress(self.tail_log(inst.engine, 6))
        return {"window": out, "live": live, "since_start": {
            "gen_tokens": t["gen_tokens"], "prompt_tokens": t["prompt_tokens"], "cached_tokens": t["cached_tokens"],
            "requests": t["requests"],
            "gen_tps": t["gen_tokens"] / t["gen_seconds"] if t["gen_seconds"] else None,
            "prompt_tps": t["prompt_tokens"] / t["prompt_seconds"] if t["prompt_seconds"] else None},
            "gauges": inst.gauges}

    def status(self) -> dict:
        snap = self.snap
        engines = []
        for name, eng in self.engines.items():
            inst = self.instances.get(name)
            e = {**eng.public(), "version": None, "update": self._update_info(name), "running": None}
            if inst:
                e["running"] = {"profile": inst.profile, "pid": inst.pid, "state": inst.state, "managed": inst.ours,
                                "started": inst.started, "ready": inst.ready,
                                "uptime_seconds": time.time() - inst.started, "ram_mib": inst.ram_mib,
                                "cpu_percent": inst.cpu_percent, "vram_mib_est": self._instance_vram(inst),
                                "vram_measured": inst.vram_measured,
                                "context": inst.context,
                                "context_source": inst.context_source,
                                "launched_context": inst.launched_context,
                                "configured_context": self._context(inst.profile),
                                "stats": self.live(inst),
                                "vision": self._vision(name, inst.profile),
                                "reasoning": self._reasoning_now(name) if inst.state == "ready" else None}
            else:
                owner = None
                try:
                    o = resources.port_owner(eng.port)
                    owner = {"pid": o.pid, "name": o.name()} if o else None
                except psutil.Error:
                    pass
                e["port_taken_by"] = owner
                e["last"] = self._last_run(name)
            e["version"] = e["update"].get("current")
            engines.append(e)
        warn = []
        if snap["ram_percent"] >= self.cfg["resources"]["warn_ram_percent"]:
            warn.append(f"RAM is {snap['ram_percent']:.0f}% used")
        st = {"time": time.time(), "machine": machine_info(self.cfg), "budget": self.budget_public(),
              "engines": engines, "resources": snap, "warnings": warn,
              "job": self.current_job.public(10) if self.current_job else None,
              "queued": self.queue.qsize(),
              "main_follows_running": self.roles.follows_running() if getattr(self, "roles", None) else True,
              "roles": self.roles.public() if getattr(self, "roles", None) else []}
        gm = gpumonitor.status()
        if gm:
            st["gpumonitor"] = gm
        return st

    def _context(self, profile: str):
        from .engines.base import arg_value
        try:
            p = self.profiles.get(profile)
            eng = self.engines.get(p.engine) if p else None
            if not eng:
                return None
            if eng.plugin == "llamacpp":
                args = [str(a) for a in p.cfg.get("args", [])]
                return eng.launch_context((["-c", str(p.cfg["ctx"])] if p.cfg.get("ctx") else []) + args)
            value = (arg_value(eng._run_config(p).get("args", []), "--max-context")
                     if eng.plugin == "strata" else p.cfg.get("ctx"))
            ctx = int(value or 0)
            return ctx if ctx > 0 else None
        except (KeyError, TypeError, ValueError):
            return None

    def profiles_public(self) -> list[dict]:
        self.refresh_profiles()
        with self.lock:
            profiles = sorted(self.profiles.values(), key=lambda x: (x.engine, x.id))
            running = {i.profile for i in self.instances.values()}
        measurements = self.store.learned_many([p.id for p in profiles], {p.id: self._profile_key(p) for p in profiles})
        out = []
        for p in profiles:
            d = p.public()
            d["running"] = p.id in running
            d["vision"] = self._vision(p.engine, p.id)
            try:
                d["needs"] = self.needs(p, measurements)
            except Exception as e:  # noqa: BLE001
                d["needs"] = {"error": str(e)}
            out.append(d)
        return out


def _redact_command(cmd: list[str]) -> list[str]:
    sensitive = {"--api-key", "--token", "--hf-token", "--access-token", "--password", "--secret"}
    out, hide_next = [], False
    for arg in cmd:
        option, sep, _ = arg.partition("=")
        if hide_next:
            out.append("<redacted>")
            hide_next = False
        elif option.lower().replace("_", "-") in sensitive:
            out.append(option + "=<redacted>" if sep else option)
            hide_next = not sep
        else:
            out.append(arg)
    return out


def _kill_tree(pid: int, timeout: float = 15):
    try:
        root = psutil.Process(pid)
        procs = root.children(recursive=True) + [root]
    except psutil.NoSuchProcess:
        return
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
