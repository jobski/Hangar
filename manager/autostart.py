"""Start Hangar when you sign in to Windows: a shortcut in your Startup folder (no admin rights).

Only the manager starts; engines start on demand as usual. Turning it on does not start a second manager now; it
takes effect at the next sign-in.

Windows only. On macOS a LaunchAgent would do it, but endpoint security tools (SentinelOne, for one) quarantine
LaunchAgents written by scripts, so Hangar does not make one; start it with scripts/start.sh. Turning it off still
removes the LaunchAgent earlier versions wrote.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WINDOWS = os.name == "nt"

STARTUP = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
LINK = STARTUP / "Hangar.lnk"
OLD_LINK = STARTUP / "LLM Manager.lnk"          # the name before the rename
START_PS1 = ROOT / "scripts" / "start.ps1"
OLD_AGENT = Path.home() / "Library" / "LaunchAgents" / "dev.hangar.manager.plist"     # written by earlier macOS versions

UNSUPPORTED = ("starting with the system is only built in on Windows. On macOS, security tools such as SentinelOne "
               "quarantine LaunchAgents, so start Hangar with scripts/start.sh (or add it to Login Items yourself).")


def status() -> dict:
    if not WINDOWS:
        return {"supported": False, "enabled": False, "note": UNSUPPORTED, "leftover": str(OLD_AGENT) if OLD_AGENT.exists() else None,
                "platform": "macos" if sys.platform == "darwin" else sys.platform}
    out = {"supported": True, "enabled": LINK.exists(), "shortcut": str(LINK), "runs": str(START_PS1), "platform": "windows"}
    if LINK.exists():
        target = _read_link(LINK)
        out["stale"] = bool(target) and str(START_PS1).lower() not in target.lower()
    return out


def enable() -> dict:
    if not WINDOWS:
        raise ValueError(UNSUPPORTED)
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
        del s                       # release the COM object before CoUninitialize
    finally:
        pythoncom.CoUninitialize()
    OLD_LINK.unlink(missing_ok=True)
    return status()


def disable() -> dict:
    if WINDOWS:
        LINK.unlink(missing_ok=True)
        OLD_LINK.unlink(missing_ok=True)
    else:
        OLD_AGENT.unlink(missing_ok=True)
    return status()


def _read_link(link: Path) -> str:
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            s = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(link))
            target = f"{s.TargetPath} {s.Arguments}"
            del s
            return target
        finally:
            pythoncom.CoUninitialize()
    except Exception:  # noqa: BLE001 - a shortcut we cannot read is just reported as not stale
        return ""
