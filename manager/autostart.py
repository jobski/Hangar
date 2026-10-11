"""Start Hangar when you sign in: a Startup-folder shortcut on Windows, a LaunchAgent on macOS. No admin rights.

Only the manager starts; engines start on demand as usual. Turning it on does not start a second manager now; it
takes effect at the next sign-in.
"""
from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAC = sys.platform == "darwin"
WINDOWS = os.name == "nt"

# Windows
STARTUP = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
LINK = STARTUP / "Hangar.lnk"
OLD_LINK = STARTUP / "LLM Manager.lnk"          # the name before the rename
START_PS1 = ROOT / "scripts" / "start.ps1"

# macOS
LABEL = "dev.hangar.manager"
AGENT = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
PYTHON = ROOT / ".venv" / "bin" / "python"


def _where() -> Path:
    if WINDOWS:
        return LINK
    if MAC:
        return AGENT
    raise RuntimeError("starting with the system is supported on Windows and macOS; on Linux use a systemd user unit "
                       "that runs: .venv/bin/python -m manager serve")


def status() -> dict:
    where = _where()
    out = {"enabled": where.exists(), "shortcut": str(where), "runs": str(START_PS1 if WINDOWS else PYTHON),
           "platform": "windows" if WINDOWS else "macos"}
    if where.exists():
        target = _read_link(where) if WINDOWS else _read_agent()
        out["stale"] = bool(target) and str(START_PS1 if WINDOWS else ROOT).lower() not in target.lower()
    return out


def enable() -> dict:
    _enable_windows() if WINDOWS else _enable_mac() if MAC else _where()
    return status()


def disable() -> dict:
    _where().unlink(missing_ok=True)
    if WINDOWS:
        OLD_LINK.unlink(missing_ok=True)
    return status()


# ---- Windows: a shortcut that runs scripts/start.ps1 hidden (the same one scripts/install-autostart.ps1 makes)
def _enable_windows():
    import pythoncom
    import win32com.client
    STARTUP.mkdir(parents=True, exist_ok=True)
    pythoncom.CoInitialize()        # API calls run on worker threads
    try:
        s = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(LINK))
        s.TargetPath = "powershell.exe"
        s.Arguments = f'-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{START_PS1}"'
        s.WorkingDirectory = str(ROOT)
        s.WindowStyle = 7           # minimized; start.ps1 then runs the manager with no window
        s.Description = "Hangar"
        s.Save()
        del s
    finally:
        pythoncom.CoUninitialize()
    OLD_LINK.unlink(missing_ok=True)


def _read_link(link: Path) -> str:
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            s = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(link))
            target = f"{s.TargetPath} {s.Arguments}"
            del s                   # release the COM object before CoUninitialize
            return target
        finally:
            pythoncom.CoUninitialize()
    except Exception:  # noqa: BLE001 - a shortcut we cannot read is just reported as not stale
        return ""


# ---- macOS: a LaunchAgent that runs the manager in the foreground at login (launchd keeps its output in logs/)
def _enable_mac():
    AGENT.parent.mkdir(parents=True, exist_ok=True)
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(PYTHON), "-m", "manager", "serve"],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",          # not throttled like a background daemon
        "StandardOutPath": str(logs / "manager.log"),
        "StandardErrorPath": str(logs / "manager.err.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
    }
    AGENT.write_bytes(plistlib.dumps(plist))


def _read_agent() -> str:
    try:
        d = plistlib.loads(AGENT.read_bytes())
        return " ".join(d.get("ProgramArguments", [])) + " " + d.get("WorkingDirectory", "")
    except Exception:  # noqa: BLE001
        return ""
