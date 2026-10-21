"""Identify the running manager, independently of later checkout updates."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from . import __version__
from .config import ROOT, write_json


def runtime_version(data_dir: Path) -> dict:
    now = time.time()
    info = {"version": __version__, "revision": None, "dirty": None,
            "committed_at": None, "updated_at": None, "started_at": now}

    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                              timeout=3, check=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()

    try:
        revision, committed = git("show", "-s", "--format=%H%n%ct", "HEAD").splitlines()
        info.update(revision=revision, committed_at=int(committed))
        info["dirty"] = bool(git("status", "--porcelain", "--untracked-files=normal", "--", "manager"))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    # Record when this build first ran; a routine restart retains its update time.
    identity = {k: info[k] for k in ("version", "revision", "dirty")}
    path = data_dir / "version.json"
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    if all(previous.get(k) == v for k, v in identity.items()):
        updated = previous.get("updated_at")
        if isinstance(updated, (int, float)):
            info["updated_at"] = updated
    if info["updated_at"] is None:
        try:
            write_json(path, {**identity, "updated_at": now})
            info["updated_at"] = now
        except OSError:
            pass
    return info
