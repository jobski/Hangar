"""mlx-lm's OpenAI-compatible server on Apple Silicon (pip install mlx-lm; github.com/ml-explore/mlx-lm).

    engines:
      mlx:
        plugin: mlx
        python: ~/.venvs/mlx/bin/python      # the Python that has mlx-lm installed
        port: 8082
    profiles:
      qwen3-coder-30b-mlx:
        engine: mlx
        model: mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit    # a Hugging Face repo or a local folder
        args: [--max-tokens, "8192"]

Every MLX model already in the Hugging Face cache becomes a profile on its own (mlx-<name>). The server has no API
key and no metrics page, so Hangar shows it running, its memory and its log, without token counts. Updates install
the newest mlx-lm from PyPI into that Python; rollback reinstalls the version before.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import httpx

from .base import Engine, Estimate, Launch, Profile, arg_value

PYPI = "https://pypi.org/pypi/mlx-lm/json"


def hf_cache() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    return Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface") / "hub"


class MlxEngine(Engine):
    plugin = "mlx"
    can_update = True
    can_rollback = True
    metrics_path = None

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        self.python = str(cfg.get("python") or "python3")
        self.command = [str(x) for x in cfg.get("command") or [self.python, "-m", "mlx_lm.server"]]
        self.startup_timeout_s = int(cfg.get("startup_timeout_s", 900))      # first run downloads the model
        self.cache = Path(cfg["hf_cache"]) if cfg.get("hf_cache") else hf_cache()
        self.state = Path(cfg.get("_data_dir", ".")) / f"{name}-versions.json"

    # ---- profiles
    def discover_profiles(self):
        out = {}
        if not self.cache.is_dir():
            return out
        for d in sorted(self.cache.glob("models--*--*")):
            org, repo = d.name[len("models--"):].split("--", 1)
            snap = _latest_snapshot(d)
            if not snap or not _is_mlx(org, snap):
                continue
            pid = "mlx-" + repo.lower()
            out[pid] = Profile(id=pid, engine=self.name, description=f"{org}/{repo} (MLX)", discovered=True,
                               cfg={"model": f"{org}/{repo}", **(self.cfg.get("profile_defaults") or {})},
                               policy=(self.cfg.get("profile_defaults") or {}).get("policy", "exclusive"))
        return out

    # ---- running
    def launch(self, profile):
        c = profile.cfg
        cmd = self.command + ["--model", str(c["model"]), "--host", self.host, "--port", str(self.port)]
        return Launch(cmd=cmd + [str(a) for a in c.get("args", [])], cwd=c.get("cwd"),
                      env={k: str(v) for k, v in (c.get("env") or {}).items()})

    def health(self, profile):
        # the server loads the --model before it starts listening, so answering means ready
        for path in ("/health", "/v1/models"):
            try:
                r = httpx.get(self.base_url() + path, timeout=5)
            except httpx.HTTPError as e:
                return False, f"not answering ({type(e).__name__})"
            if r.status_code == 200:
                return True, "HTTP 200"
        return False, f"HTTP {r.status_code}"

    def match_process(self, cmdline, profiles):
        if not any("mlx_lm" in a for a in cmdline):
            return None
        model = arg_value(cmdline, "--model")
        for p in profiles.values():
            if p.engine == self.name and str(p.cfg.get("model")) == model:
                return p.id
        return None

    def estimate(self, profile, vram_total_mib, ram_total_mib):
        r = profile.cfg.get("requires") or {}
        if r:
            return super().estimate(profile, vram_total_mib, ram_total_mib)
        size = _model_mib(str(profile.cfg.get("model", "")), self.cache)
        if not size:
            return Estimate(0, 0, False, "model not downloaded yet: size unknown until the first run")
        # weights live in unified memory; leave ~15% for the KV cache and activations
        return Estimate(size * 1.15, 512, False, "estimated from the model files; replaced by measured use after a run")

    # ---- updates: mlx-lm from PyPI, into the configured Python
    def version(self):
        try:
            out = subprocess.run([self.python, "-c", "import importlib.metadata as m; print(m.version('mlx-lm'))"],
                                 capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() or None

    def check_update(self, log):
        cur = self.version()
        latest = httpx.get(PYPI, timeout=30).json()["info"]["version"]
        available = bool(latest and cur != latest and _newer(latest, cur))
        log(f"{self.name}: " + (f"mlx-lm {cur} installed" if cur else "mlx-lm not installed") + f", {latest} on PyPI"
            + (" - update available" if available else " - up to date"))
        return {"current": cur, "latest": latest, "available": available}

    def _pip(self, spec: str, log):
        log(f"  {self.python} -m pip install {spec}")
        p = subprocess.run([self.python, "-m", "pip", "install", "--upgrade", spec], capture_output=True, text=True,
                           timeout=1800)
        for line in (p.stdout + p.stderr).splitlines()[-15:]:
            log("  " + line)
        if p.returncode != 0:
            raise RuntimeError(f"pip install {spec} failed (exit code {p.returncode})")

    def update(self, log):
        info = self.check_update(log)
        if not info["available"]:
            return {**info, "updated": False}
        if info["current"]:
            self.state.write_text(json.dumps({"previous": info["current"]}))
        self._pip(f"mlx-lm=={info['latest']}", log)
        log(f"{self.name}: mlx-lm is now {self.version()}")
        return {**info, "updated": True, "current": self.version()}

    def rollback(self, log):
        prev = json.loads(self.state.read_text()).get("previous") if self.state.exists() else None
        if not prev:
            raise RuntimeError("nothing to roll back to: no earlier mlx-lm version was recorded by an update")
        cur = self.version()
        self._pip(f"mlx-lm=={prev}", log)
        if cur:
            self.state.write_text(json.dumps({"previous": cur}))
        log(f"{self.name}: rolled back to mlx-lm {self.version()}")
        return {"current": self.version()}


def _latest_snapshot(repo_dir: Path) -> Path | None:
    ref = repo_dir / "refs" / "main"
    if ref.exists():
        snap = repo_dir / "snapshots" / ref.read_text().strip()
        if snap.is_dir():
            return snap
    snaps = sorted((repo_dir / "snapshots").glob("*"), key=lambda p: p.stat().st_mtime) if (repo_dir / "snapshots").is_dir() else []
    return snaps[-1] if snaps else None


def _is_mlx(org: str, snap: Path) -> bool:
    """An MLX model: safetensors weights, and either an MLX org/name or MLX's quantization block in config.json."""
    if not any(snap.glob("*.safetensors")):
        return False
    if "mlx" in org.lower() or "mlx" in snap.parent.parent.name.lower():
        return True
    try:
        cfg = json.loads((snap / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    q = cfg.get("quantization") or {}
    return isinstance(q, dict) and "group_size" in q and "bits" in q


def _model_mib(model: str, cache: Path) -> float:
    """Size of the weights: a local folder, or the repo's snapshot in the Hugging Face cache."""
    p = Path(os.path.expanduser(model))
    if not p.is_dir() and "/" in model:
        org, repo = model.split("/", 1)
        snap = _latest_snapshot(cache / f"models--{org}--{repo}")
        p = snap if snap else p
    if not p.is_dir():
        return 0.0
    return sum(f.stat().st_size for f in p.glob("*.safetensors")) / 2**20     # snapshot files resolve to the blobs


def _newer(a: str, b: str | None) -> bool:
    if not b:
        return True
    try:
        return tuple(int(x) for x in a.split(".")[:3]) > tuple(int(x) for x in b.split(".")[:3])
    except ValueError:
        return a != b
