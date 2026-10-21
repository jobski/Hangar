"""Project Maya: Strata-compatible API, with Maya's own profiles and startup checks."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .base import Engine, Estimate, arg_value, file_mib
from .strata import StrataEngine


GEN = re.compile(r"\[maya\]\s+(?P<phase>.+?):\s*(?P<n>[\d,]+)\s+of\s+(?:max|budget)\s+[\d,]+"
                 r"(?:\s+\(max\s+[\d,]+\))?\s+tokens,\s*(?P<tps>\d+(?:\.\d+)?)\s+tok/s", re.I)
READ = re.compile(r"\[maya\] reading the prompt: (?P<n>[\d,]+)(?: of (?P<total>[\d,]+))? tokens, "
                  r"(?P<s>\d+) s so far")


def pack_sources(pack: Path) -> list[Path]:
    """Backing GGUFs named by Maya's pack manifest (the pack itself is mostly an index)."""
    try:
        manifest = json.loads((pack / "sources.json").read_text(encoding="utf-8"))
        return [pack.parent / entry["name"] for entry in manifest["files"]]
    except (OSError, ValueError, KeyError, TypeError):
        return []


class MayaEngine(StrataEngine):
    plugin = "maya"
    display_name = "Maya"
    config_glob = "maya-*.json"
    LEVELS = ["default", "none", "low", "high", "max"]

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        self.can_update = False  # Maya's release/rebuild lifecycle needs a separate updater.
        if not cfg.get("python") and os.name != "nt":
            self.python = self.dir / ".venv" / "bin" / "python"

    def _server_command(self, config):
        # Keep Maya's model validation, config migrations and incremental compilation.
        # --plain avoids its interactive terminal UI; --yes makes startup unattended.
        cmd = [str(self.python), str(self.dir / "maya.py"), "--plain", "--yes",
               "--config", str(config), "--port", str(self.port)]
        if self.api_key:
            cmd += ["--api-key", self.api_key]
        return cmd

    def has_vision(self, profile):
        return bool(self._run_config(profile).get("vision"))

    def estimate(self, profile, vram_total_mib, ram_total_mib):
        if profile.cfg.get("requires"):
            return Engine.estimate(self, profile, vram_total_mib, ram_total_mib)
        rc = self._run_config(profile)
        args = rc.get("args") or []
        files = []
        for flag in ("--glm-pack", "--pack"):
            value = arg_value(args, flag)
            if value:
                pack = self.dir / value
                files.extend(pack_sources(pack))
                if pack.is_dir():
                    files.extend(p for p in pack.rglob("*") if p.is_file())
        native = arg_value(args, "--native")
        if native:
            path = self.dir / native
            stem = path.name.split("-00001-of-")[0] if "-00001-of-" in path.name else None
            files.extend(path.parent.glob(stem + "-*-of-*.gguf") if stem else [path])
        return Estimate(vram_total_mib, min(file_mib(*files), ram_total_mib * 0.85), True,
                        "Maya uses the GPUs and streams the remaining experts from RAM/disk; "
                        "RAM is an estimate until a run is measured")

    def progress(self, lines):
        for line in reversed(lines):
            if "[maya] done:" in line:
                return None
            match = GEN.search(line)
            if match:
                return {"phase": match["phase"], "gen_tps": float(match["tps"]),
                        "tokens": int(match["n"].replace(",", ""))}
            match = READ.search(line)
            if match:
                n, seconds = int(match["n"].replace(",", "")), int(match["s"])
                result = {"phase": "reading the prompt", "prompt_tps": n / seconds if seconds else None, "tokens": n}
                if match["total"]:
                    result["of"] = int(match["total"].replace(",", ""))
                return result
        return None
