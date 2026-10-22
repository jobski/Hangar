"""Project Maya: Strata-compatible API, with Maya's own profiles and startup checks."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

import httpx
import psutil

from .base import Engine, Estimate, arg_value, file_mib
from .strata import NO_WINDOW, StrataEngine


GEN = re.compile(r"\[maya\]\s+(?P<phase>.+?):\s*(?P<n>[\d,]+)\s+of\s+(?:max|budget)\s+[\d,]+"
                 r"(?:\s+\(max\s+[\d,]+\))?\s+tokens,\s*(?P<tps>\d+(?:\.\d+)?)\s+tok/s", re.I)
READ = re.compile(r"\[maya\] reading the prompt: (?P<n>[\d,]+)(?: of (?P<total>[\d,]+))? tokens, "
                  r"(?P<s>\d+) s so far")


# A new process group (how the core starts engines on Windows) begins with Ctrl+C off for everything in it; this turns
# it back on before maya.py runs, so a clean stop - Ctrl+C on its console, as in a terminal - reaches it.
CTRL_C_ON = ("import ctypes, runpy, sys; ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False); "
             "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')")
# Ctrl+C to every process on Maya's console. A process of its own: attaching would take the manager off its console.
# (CREATE_NO_WINDOW still gives Maya a console, only without a window: AttachConsole works - checked on Windows 11.)
CTRL_C_SEND = ("import ctypes, sys; k = ctypes.WinDLL('kernel32'); k.FreeConsole(); "
               "sys.exit(0 if k.AttachConsole(int(sys.argv[1])) and k.SetConsoleCtrlHandler(None, True) "
               "and k.GenerateConsoleCtrlEvent(0, 0) else 1)")
STOP_WAIT_S = 160   # maya.py's own stop allows 150 s: the request in progress, then the engine's QUIT and save
# Windows' engine does not ignore Ctrl+C (Maya's generate.cpp ignores it on POSIX only), so it ends before its QUIT
# save; a request that does not continue the conversation it holds makes it set that one aside on disk first.
SET_ASIDE = {"model": "hangar", "max_tokens": 1, "stream": False,
             "messages": [{"role": "user", "content": "Hangar is stopping this model: reply with one word."}]}


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
        cmd = [str(self.python), *(["-c", CTRL_C_ON] if os.name == "nt" else []), str(self.dir / "maya.py"),
               "--plain", "--yes", "--config", str(config), "--port", str(self.port)]
        if self.api_key:
            cmd += ["--api-key", self.api_key]
        return cmd

    def launch(self, profile):
        launch = super().launch(profile)
        if profile.cfg.get("keep_conversations", True):        # its conversations outlive a switch (disk slots)
            launch.env.setdefault("STRATA_GLM_SLOT_KEEP", "1")
        return launch

    def stop_cleanly(self, pid, log):
        """As Ctrl+C in Maya's terminal: the reply in progress ends, the engine gets QUIT and keeps its conversation
        (with STRATA_GLM_SLOT_KEEP, on disk). Ending the process tree at once would lose the one the engine holds."""
        try:
            if os.name == "nt":
                self._set_aside(log)
                py = Path(sys.executable).with_name("python.exe")
                sent = subprocess.run([str(py if py.exists() else sys.executable), "-c", CTRL_C_SEND, str(pid)],
                                      creationflags=NO_WINDOW, timeout=15).returncode == 0
            else:
                os.kill(pid, signal.SIGTERM)                   # maya.py closes the engine's pipe: the same clean stop
                sent = True
        except (OSError, subprocess.SubprocessError):
            sent = False
        if not sent:
            return False
        log(f"asked Maya to stop cleanly, keeping its conversation (up to {STOP_WAIT_S} s)")
        try:
            psutil.Process(pid).wait(STOP_WAIT_S)
        except psutil.NoSuchProcess:
            pass
        except psutil.TimeoutExpired:
            log(f"Maya did not stop within {STOP_WAIT_S} s: ending it")
            return False
        return True

    def _set_aside(self, log):
        """Have the engine put the conversation it holds on disk before a stop that ends it at once (Windows)."""
        if self.active_profile is not None and not self.active_profile.cfg.get("keep_conversations", True):
            return
        try:
            r = httpx.post(self.base_url() + "/v1/chat/completions", json=SET_ASIDE, headers=self.auth_headers(),
                           timeout=30)
            log("set Maya's conversation aside on disk" if r.status_code == 200 else
                f"could not set Maya's conversation aside (HTTP {r.status_code})")
        except httpx.HTTPError as e:
            log(f"could not set Maya's conversation aside ({type(e).__name__})")

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
