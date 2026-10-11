"""Start Hangar when you sign in to Windows: a shortcut in your Startup folder (no admin needed).

The shortcut runs scripts/start.ps1 hidden, the same thing scripts/install-autostart.ps1 sets up. Engines are not
started with it; profiles start on demand as usual.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STARTUP = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
LINK = STARTUP / "Hangar.lnk"
OLD_LINK = STARTUP / "LLM Manager.lnk"          # the name before the rename


def status() -> dict:
    out = {"enabled": LINK.exists(), "shortcut": str(LINK), "runs": str(ROOT / "scripts" / "start.ps1")}
    if LINK.exists():
        target = _read(LINK)
        out["stale"] = bool(target) and str(ROOT / "scripts" / "start.ps1").lower() not in target.lower()
    return out


def enable() -> dict:
    import pythoncom
    import win32com.client
    STARTUP.mkdir(parents=True, exist_ok=True)
    pythoncom.CoInitialize()        # API calls run on worker threads
    try:
        s = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(LINK))
        s.TargetPath = "powershell.exe"
        s.Arguments = f'-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{ROOT / "scripts" / "start.ps1"}"'
        s.WorkingDirectory = str(ROOT)
        s.WindowStyle = 7           # minimized; start.ps1 then runs the manager with no window
        s.Description = "Hangar"
        s.Save()
    finally:
        pythoncom.CoUninitialize()
    OLD_LINK.unlink(missing_ok=True)
    return status()


def disable() -> dict:
    LINK.unlink(missing_ok=True)
    OLD_LINK.unlink(missing_ok=True)
    return status()


def _read(link: Path) -> str:
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            s = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(link))
            return f"{s.TargetPath} {s.Arguments}"
        finally:
            pythoncom.CoUninitialize()
    except Exception:  # noqa: BLE001 - a shortcut we cannot read is just reported as not stale
        return ""
