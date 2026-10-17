"""Loads config.yaml: the manager's own settings, the engines and the profiles."""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def write_json(path: Path, data, indent: int = 1):
    """Write JSON so a crash or a reader at the wrong moment never sees half a file: a temp file, then a rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")   # one per writer
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=indent))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

DEFAULTS = {
    "manager": {"host": "0.0.0.0", "port": 9000, "data_dir": "data", "logs_dir": "logs"},
    "resources": {
        "vram_reserve_mib": 512,       # keep this much VRAM spare when deciding whether something fits
        "ram_reserve_mib": 4096,
        "vram_release_timeout_s": 60,  # after a stop, how long to wait for the driver to hand VRAM back
        "sample_interval_s": 5,
        "warn_ram_percent": 92,
    },
    "updates": {
        "auto_before_start": True,     # check (and apply) engine updates before starting an engine
        "min_interval_hours": 6,       # ...but at most this often per engine
        "background_check_hours": 6,   # background check that only reports
    },
    "models": {"dir": "E:/models", "scan": []},
    "downloads": {"max_parallel": 2, "aria2": "auto", "aria2_connections": 16},
    "templates": {},
    "engines": {},
    "profiles": {},
}

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _expand(v):
    """${VAR} in any string is replaced by that environment variable (handy for keys kept out of the file), and a
    leading ~/ is the home folder (macOS paths are mostly under it)."""
    if isinstance(v, str):
        v = _ENV.sub(lambda m: os.environ.get(m.group(1), ""), v)
        return os.path.expanduser(v) if v.startswith(("~/", "~\\")) or v == "~" else v
    if isinstance(v, list):
        return [_expand(x) for x in v]
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    return v


def config_path() -> Path:
    return Path(os.environ.get("LLM_MANAGER_CONFIG") or ROOT / "config.yaml")


def load() -> dict:
    p = config_path()
    if not p.exists():
        raise FileNotFoundError(f"{p} not found: copy config.example.yaml to config.yaml and edit it")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = _expand(_merge(DEFAULTS, raw))
    for key in ("data_dir", "logs_dir"):
        d = Path(cfg["manager"][key])
        cfg["manager"][key] = d if d.is_absolute() else ROOT / d
        cfg["manager"][key].mkdir(parents=True, exist_ok=True)
    return cfg


def token(cfg: dict) -> str:
    """The manager's API token: LLM_MANAGER_TOKEN, else data/token.txt (made on first run)."""
    if os.environ.get("LLM_MANAGER_TOKEN"):
        return os.environ["LLM_MANAGER_TOKEN"]
    f = cfg["manager"]["data_dir"] / "token.txt"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return f.read_text(encoding="utf-8").strip()
