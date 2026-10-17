"""Installer and service commands bound to this checkout, independent of saved CLI connections."""
from __future__ import annotations

import os
import sys

import httpx

from . import config, resources


def main():
    cfg = config.load()
    port = int(cfg["manager"]["port"])
    if sys.argv[1:] == ["--port"]:
        print(port)
        return
    token_file = cfg["manager"]["data_dir"] / "token.txt"
    token = os.environ.get("LLM_MANAGER_TOKEN") or (token_file.read_text().strip() if token_file.exists() else "")
    url = f"http://127.0.0.1:{port}"
    try:
        response = httpx.get(url + "/api/jobs?limit=1", headers={"Authorization": f"Bearer {token}"}, timeout=3)
        ours = bool(token) and response.status_code == 200
    except httpx.HTTPError:
        ours = False
    if sys.argv[1:] == ["--owns"]:
        raise SystemExit(0 if ours else 1)
    if not ours:
        raise SystemExit("No authenticated manager from this checkout is listening; nothing was stopped.")
    if sys.argv[1:] == ["--stop-manager"]:
        owner = resources.port_owner(port, fresh=True)
        if owner is None or owner.pid is None:
            raise SystemExit("Cannot identify this manager process; nothing was stopped.")
        owner.terminate()
        try:
            owner.wait(timeout=15)
        except resources.psutil.TimeoutExpired:
            raise SystemExit("Manager did not stop within 15 seconds.")
        print("Hangar stopped (engines left running).")
        return
    os.environ["HANGAR_URL"], os.environ["HANGAR_TOKEN"] = url, token
    from . import cli
    raise SystemExit(cli.main(sys.argv[1:]))


if __name__ == "__main__":
    main()
