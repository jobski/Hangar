"""Strata (github.com/Niko1221/Strata).

Each installed model has a strata-<name>.json run config in the Strata folder; every one becomes a profile on its
own. The server is started the way Strata's run-*.bat scripts start it. Updates run Strata's own UPDATE.bat (git pull,
then setup --update), which never starts the model.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import threading
from pathlib import Path

import httpx
import psutil

from .base import Engine, Estimate, Launch, Profile, arg_value, file_mib

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p.strip('"')))


# "[strata] thinking: 5466 of max 64000 tokens, 42.7 tok/s, 231 s" (also answering / writing a tool call: <name>)
GEN = re.compile(r"\[strata\] (?P<phase>.+?): (?P<n>[\d,]+) of max [\d,]+ tokens, (?P<tps>[\d.]+) tok/s")
# "[strata] reading the prompt: 22,982 of 39,584 tokens, 19 s so far"
READ = re.compile(r"\[strata\] reading the prompt: (?P<n>[\d,]+) of (?P<total>[\d,]+) tokens, (?P<s>\d+) s so far")


def _kill_tree(pid: int):
    """Stop a process and everything it started (a child holding our output pipe would keep the update stuck)."""
    try:
        proc = psutil.Process(pid)
        for c in proc.children(recursive=True) + [proc]:
            try:
                c.kill()
            except psutil.Error:
                pass
    except psutil.Error:
        pass


class StrataEngine(Engine):
    plugin = "strata"
    can_update = True
    metrics_path = "/metrics/prometheus"

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        self.can_update = os.name == "nt"
        self.dir = Path(cfg["dir"])
        self.python = Path(cfg.get("python") or self.dir / ".venv" / "Scripts" / "python.exe")
        self.startup_timeout_s = int(cfg.get("startup_timeout_s", 1200))   # big models stream in from disk
        self.update_timeout_s = int(cfg.get("update_timeout_s", 7200))
        self.active_profile: Profile | None = None

    # ---- profiles
    def _run_configs(self) -> list[Path]:
        return sorted(p for p in self.dir.glob("strata-*.json") if not p.name.endswith(".shared-settings.json"))

    def discover_profiles(self):
        out = {}
        for f in self._run_configs():
            try:
                rc = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if "exe" not in rc:
                continue
            pid = f.stem                                     # strata-orca-iq3_xxs
            desc = rc.get("model_name") or pid
            if rc.get("vision"):
                desc += " (vision)"
            out[pid] = Profile(id=pid, engine=self.name, description=desc, discovered=True,
                               cfg={"config": str(f), **(self.cfg.get("profile_defaults") or {})},
                               policy=(self.cfg.get("profile_defaults") or {}).get("policy", "exclusive"))
        return out

    def make_profile(self, pid, pcfg):
        p = super().make_profile(pid, pcfg)
        if not pcfg.get("config"):
            p.cfg["config"] = str(self.dir / f"{pid}.json")
        return p

    def _run_config(self, profile: Profile | None) -> dict:
        if not profile:
            return {}
        try:
            from ..config import read_json
            value = read_json(Path(profile.cfg["config"]))
            if not isinstance(value, dict):
                return {}
            if "args" in profile.cfg:
                value["args"] = list(profile.cfg["args"])
            if profile.cfg.get("ctx"):
                args, result = list(value.get("args") or []), []
                skip = False
                for arg in args:
                    if skip:
                        skip = False
                    elif arg == "--max-context":
                        skip = True
                    elif not str(arg).startswith("--max-context="):
                        result.append(arg)
                value["args"] = result + ["--max-context", str(profile.cfg["ctx"])]
            if profile.cfg.get("reasoning_effort"):
                value["reasoning_effort"] = profile.cfg["reasoning_effort"]
            return value
        except (OSError, ValueError, KeyError):
            return {}

    # ---- running
    def launch(self, profile):
        config = profile.cfg["config"]
        if any(key in profile.cfg for key in ("args", "ctx", "reasoning_effort")):
            from ..config import write_json
            folder = Path(self.cfg.get("_data_dir") or self.dir) / "custom-configs"
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            config = folder / (hashlib.sha256(profile.id.encode()).hexdigest()[:24] + ".json")
            write_json(config, self._run_config(profile))
        cmd = [str(self.python), str(self.dir / "serve" / "server.py"), "--engine", "strata",
               "--config", str(config), "--port", str(self.port)]
        if self.cfg.get("host"):
            cmd += ["--host", self.host]
        cmd += [str(a) for a in profile.cfg.get("server_args", [])]
        return Launch(cmd=cmd, cwd=str(self.dir), env={k: str(v) for k, v in (profile.cfg.get("env") or {}).items()})

    def _context_config(self, cmdline, cwd=None):
        config = arg_value(cmdline, "--config")
        if not config:
            return None
        path = Path(config)
        return path if path.is_absolute() else Path(cwd or self.dir) / path

    def launch_context(self, cmdline):
        try:
            from ..config import read_json
            path = self._context_config(cmdline)
            args = read_json(path).get("args", []) if path else []
            ctx = int(arg_value(args, "--max-context") or 0)
            return ctx if ctx > 0 else None
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def process_context(self, cmdline, created, cwd=None):
        try:
            config = arg_value(cmdline, "--config")
            if not config or not Path(config).is_absolute() and not cwd:
                return None
            path = self._context_config(cmdline, cwd)
            if path.stat().st_mtime > created:
                return None
            return self.launch_context(["--config", str(path)])
        except OSError:
            return None

    def auth_headers(self):
        key = self.api_key or self._run_config(self.active_profile).get("api_key") or ""
        if isinstance(key, list):
            key = key[0] if key else ""
        key = key.split(",")[0].strip()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def health(self, profile):
        try:
            r = httpx.get(self.base_url() + "/health", headers=self.auth_headers(), timeout=5)
        except httpx.HTTPError as e:
            return False, f"not answering ({type(e).__name__})"
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        try:
            loaded = r.json().get("loaded", True)
        except ValueError:
            loaded = True
        return bool(loaded), "ready" if loaded else "server up, model loading"

    def match_process(self, cmdline, profiles):
        cfg = arg_value(cmdline, "--config")
        if not cfg:
            return None
        for p in profiles.values():
            if p.engine != self.name or not p.cfg.get("config"):
                continue
            generated = (Path(self.cfg.get("_data_dir") or self.dir) / "custom-configs"
                         / (hashlib.sha256(p.id.encode()).hexdigest()[:24] + ".json"))
            if _norm(p.cfg["config"]) == _norm(cfg) or _norm(str(generated)) == _norm(cfg):
                return p.id
        return None

    def estimate(self, profile, vram_total_mib, ram_total_mib):
        if profile.cfg.get("requires"):
            return super().estimate(profile, vram_total_mib, ram_total_mib)
        rc = self._run_config(profile)
        args = rc.get("args", [])
        native = arg_value(args, "--native")
        shards = []
        if native:
            p = self.dir / native
            stem = p.name.split("-00001-of-")[0] if "-00001-of-" in p.name else None
            shards = list(p.parent.glob(stem + "-*-of-*.gguf")) if stem else [p]
        files = file_mib(*shards)
        # Strata fills the GPU up to its --vram-reserve-mib and keeps as much of the model in RAM as fits
        return Estimate(vram_total_mib, min(files, ram_total_mib * 0.85), True,
                        "Strata uses the whole GPU and streams the rest of the model from RAM/disk")

    # ---- reasoning effort: live through Strata's shared Chat settings (POST /settings), which it also keeps on disk;
    # the run config's "reasoning_effort" is the level when those name none
    LEVELS = ["default", "none", "low", "medium", "high"]

    def has_vision(self, profile):
        rc = self._run_config(profile)
        return bool(rc.get("vision")) and "--vision" in [str(a) for a in rc.get("args", [])]

    def reasoning_levels(self, profile):
        return self.LEVELS

    def _shared(self) -> dict:
        r = httpx.get(self.base_url() + "/settings", headers=self.auth_headers(), timeout=5)
        r.raise_for_status()
        return r.json().get("defaults") or {}

    def reasoning_get(self, profile):
        try:
            active = self.active_profile
            lvl = self._shared().get("reasoning_effort") if active and active.id == profile.id else None
        except (httpx.HTTPError, ValueError):
            lvl = None
        return lvl or self._run_config(profile).get("reasoning_effort") or "default"

    def reasoning_set(self, profile, level):
        try:
            shared = self._shared()                  # keep the other Chat settings (temperature, ...) as they are
        except (httpx.HTTPError, ValueError) as e:
            raise ValueError(f"Strata is not answering ({type(e).__name__}); start it first") from e
        if level == "default":
            shared.pop("reasoning_effort", None)
        else:
            shared["reasoning_effort"] = level
        r = httpx.post(self.base_url() + "/settings", headers={**self.auth_headers(), "Content-Type": "application/json"},
                       json={"defaults": shared or None}, timeout=10)
        if r.status_code != 200:
            raise ValueError(f"Strata refused it: {r.text[:200]}")
        return True

    def progress(self, lines):
        # Strata's Prometheus counters only move when a request finishes; its log prints the live rate every second
        for line in reversed(lines):
            if "[strata] done:" in line:
                return None
            m = GEN.search(line)
            if m:
                return {"phase": m.group("phase"), "gen_tps": float(m.group("tps")), "tokens": int(m.group("n").replace(",", ""))}
            m = READ.search(line)
            if m:
                n, s = int(m.group("n").replace(",", "")), int(m.group("s"))
                return {"phase": "reading the prompt", "prompt_tps": n / s if s else None, "tokens": n,
                        "of": int(m.group("total").replace(",", ""))}
        return None

    # ---- updates
    def _git(self, *args, timeout=120) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.dir, capture_output=True, text=True, timeout=timeout,
                              creationflags=NO_WINDOW)

    def version(self):
        r = self._git("log", "-1", "--format=%h %cs")
        return r.stdout.strip() or None

    def check_update(self, log):
        if not (self.dir / ".git").exists():
            log(f"{self.name}: not a git clone - UPDATE.bat can only refresh the engine and settings")
            return {"current": None, "latest": None, "available": False, "note": "not a git clone"}
        f = self._git("fetch", "--quiet", "origin", timeout=300)
        if f.returncode != 0:
            raise RuntimeError(f"git fetch failed: {f.stderr.strip()[:300]}")
        behind = self._git("rev-list", "--count", "HEAD..@{u}").stdout.strip()
        latest = self._git("log", "-1", "--format=%h %cs", "@{u}").stdout.strip()
        n = int(behind or 0)
        log(f"{self.name}: {self.version()} installed, {latest} upstream" + (f" - {n} new commits" if n else " - up to date"))
        return {"current": self.version(), "latest": latest, "available": n > 0, "behind": n}

    def update(self, log):
        info = self.check_update(log)
        if not info["available"] and not self.cfg.get("always_run_update_bat"):
            return {**info, "updated": False}
        log(f"{self.name}: running UPDATE.bat --yes")
        p = subprocess.Popen(["cmd", "/c", "UPDATE.bat", "--yes"], cwd=self.dir, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", creationflags=NO_WINDOW)
        timed_out = threading.Event()

        def stop():
            timed_out.set()
            _kill_tree(p.pid)
        watchdog = threading.Timer(self.update_timeout_s, stop)    # the time limit holds while it is still printing
        watchdog.start()
        try:
            for line in p.stdout:
                line = line.rstrip()
                if line:
                    log("  " + line)
            p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            timed_out.set()
            _kill_tree(p.pid)
        finally:
            watchdog.cancel()
            p.stdout.close()
            if p.poll() is None:
                p.wait()
        if timed_out.is_set():
            raise RuntimeError("UPDATE.bat took too long and was stopped")
        if p.returncode != 0:
            raise RuntimeError(f"UPDATE.bat failed (exit code {p.returncode}); see the update log")
        log(f"{self.name}: updated to {self.version()}")
        return {**info, "updated": True, "current": self.version()}
