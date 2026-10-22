"""The interface every engine plugin implements.

A plugin knows how to turn a profile into a command line, how to tell when that engine is ready, how much of the
machine it is likely to need, where its token counters are, and (optionally) how to update itself. Everything else -
processes, resource checks, scheduling, stats, the API - is the core's job and works the same for every engine.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

Log = Callable[[str], None]


@dataclass
class Profile:
    id: str
    engine: str
    cfg: dict
    description: str = ""
    policy: str = "exclusive"      # exclusive | coexist | refuse  (what to do when something else is running)
    discovered: bool = False        # found by the plugin rather than written in config.yaml
    source: str = "discovered"      # discovered | config (config.yaml) | managed (profiles.yaml) | custom (one-off)

    def public(self) -> dict:
        return {"id": self.id, "engine": self.engine, "description": self.description, "policy": self.policy,
                "discovered": self.discovered, "source": self.source, "editable": self.source == "managed",
                "settings": {k: v for k, v in self.cfg.items() if k != "engine"}}


@dataclass
class Launch:
    cmd: list[str]
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Estimate:
    vram_mib: float                 # VRAM it needs to run well
    ram_mib: float                  # RAM it needs on top of that
    greedy_vram: bool = False       # takes whatever VRAM is free (llama --fit, Strata): never shares a GPU well
    note: str = ""


class Engine:
    plugin = "base"
    #: shown in the UI/MCP; actions a plugin cannot do are hidden
    can_update = False
    can_rollback = False
    startup_timeout_s = 600

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.cfg = cfg
        self.port: int = int(cfg["port"])
        self.host: str = cfg.get("host", "0.0.0.0")
        self.api_key: str = cfg.get("api_key") or ""
        self.active_profile: Profile | None = None   # set by the core while this engine runs a profile

    # ---- profiles
    def discover_profiles(self) -> dict[str, Profile]:
        """Profiles the engine finds on its own (e.g. Strata's installed models). Config profiles win on a clash."""
        return {}

    def make_profile(self, pid: str, pcfg: dict) -> Profile:
        return Profile(id=pid, engine=self.name, cfg=pcfg, description=pcfg.get("description", ""),
                       policy=pcfg.get("policy", "exclusive"))

    # ---- running
    def launch(self, profile: Profile) -> Launch:
        raise NotImplementedError

    def stop_cleanly(self, pid: int, log: Log) -> bool:
        """Ask the running process to end on its own terms; True once it has. False: the core ends it."""
        return False

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def health(self, profile: Profile | None) -> tuple[bool, str]:
        """(ready, detail). Default: GET /health answers 200."""
        try:
            r = httpx.get(self.base_url() + "/health", headers=self.auth_headers(), timeout=5)
        except httpx.HTTPError as e:
            return False, f"not answering ({type(e).__name__})"
        return r.status_code == 200, f"HTTP {r.status_code}"

    def launch_context(self, cmdline: list[str]) -> int | None:
        """An explicit context from the launched command; None when it cannot be established."""
        return None

    def process_context(self, cmdline: list[str], created: float, cwd: str | None = None) -> int | None:
        """Recover a launch limit from an existing process without trusting subsequently edited files."""
        return self.launch_context(cmdline)

    def runtime_context(self) -> int | None:
        """The live per-request token limit, when the engine exposes it."""
        return None

    def match_process(self, cmdline: list[str], profiles: dict[str, Profile]) -> str | None:
        """Which profile a running process (not started by us) is, from its command line; None if unknown."""
        return None

    def estimate(self, profile: Profile, vram_total_mib: float, ram_total_mib: float) -> Estimate:
        r = profile.cfg.get("requires") or {}
        return Estimate(float(r.get("vram_mib", 0)), float(r.get("ram_mib", 0)), bool(r.get("greedy_vram", False)))

    # ---- stats: Prometheus text with llama.cpp's metric names (llama-server --metrics, Strata /metrics/prometheus)
    metrics_path: str | None = None

    def scrape(self) -> dict[str, float] | None:
        if not self.metrics_path:
            return None
        try:
            r = httpx.get(self.base_url() + self.metrics_path, headers=self.auth_headers(), timeout=5)
            if r.status_code != 200:
                return None
        except httpx.HTTPError:
            return None
        return parse_prometheus(r.text)

    def has_vision(self, profile: Profile) -> bool:
        """Whether this profile can take images."""
        return bool(profile.cfg.get("vision"))

    # ---- reasoning effort: how hard a thinking model thinks
    def reasoning_levels(self, profile: Profile) -> list[str] | None:
        """The levels this engine can set for this profile ("default" = the model's own); None when it cannot."""
        return None

    def reasoning_get(self, profile: Profile) -> str:
        """The level in effect now. Engines that set it at launch read it from the profile (reasoning_effort)."""
        return str(profile.cfg.get("reasoning_effort") or "default")

    def reasoning_set(self, profile: Profile, level: str) -> bool:
        """Apply a level to the running engine. True when it took effect now; False when it needs a restart (the
        level is then kept with the profile and used at the next launch)."""
        return False

    def progress(self, lines: list[str]) -> dict | None:
        """What a request is doing right now, from the engine's own progress lines in its log (the last lines given,
        newest last), for engines whose counters only move when a request ends. {phase, gen_tps, tokens} or
        {phase, prompt_tps, tokens}; None when the log says nothing current."""
        return None

    # ---- updates
    def version(self) -> str | None:
        return None

    def check_update(self, log: Log) -> dict:
        """{"current": str, "latest": str, "available": bool, ...}"""
        raise NotImplementedError

    def update(self, log: Log) -> dict:
        raise NotImplementedError

    def rollback(self, log: Log) -> dict:
        raise NotImplementedError

    def public(self) -> dict:
        return {"name": self.name, "plugin": self.plugin, "port": self.port, "host": self.host,
                "can_update": self.can_update, "can_rollback": self.can_rollback}


_PROM = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+([-+0-9.eE]+|NaN|[+-]Inf)\s*$")


def parse_prometheus(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        m = _PROM.match(line.strip())
        if m:
            try:
                out[m.group(1)] = out.get(m.group(1), 0.0) + float(m.group(2))
            except ValueError:
                pass
    return out


def file_mib(*paths) -> float:
    total, seen = 0, set()
    for p in paths:
        if p and Path(p).is_file():
            path = Path(p)
            shard = re.match(r"(.+)-\d{5}-of-(\d{5})\.gguf$", path.name, re.I)
            if shard:
                files = [path.with_name(f"{shard.group(1)}-{i:05d}-of-{int(shard.group(2)):05d}.gguf")
                         for i in range(1, int(shard.group(2)) + 1)]
                if any(not f.is_file() for f in files):
                    raise ValueError(f"incomplete split model: {path.name}")
            else:
                files = [path]
            for f in files:
                identity = f.resolve()
                if identity not in seen:
                    seen.add(identity)
                    total += f.stat().st_size
    return total / 2**20


def arg_value(args: list[str], *names: str) -> str | None:
    for i, a in enumerate(args):
        if a in names and i + 1 < len(args):
            return args[i + 1]
        if any(a.startswith(name + "=") for name in names):
            return a.split("=", 1)[1]
    return None
