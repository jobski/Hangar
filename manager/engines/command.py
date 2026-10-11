"""Any program: a command line, a port and a health URL. A quick way to manage a tool before it has a real plugin.

    engines:
      comfyui:
        plugin: command
        port: 8188
        health_path: /system_stats
    profiles:
      comfyui-default:
        engine: comfyui
        cmd: [E:\\comfyui\\python_embeded\\python.exe, main.py, --listen, 0.0.0.0, --port, "8188"]
        cwd: E:\\comfyui\\ComfyUI
        requires: {vram_mib: 6000, ram_mib: 8000}
"""
from __future__ import annotations

import httpx

from .base import Engine, Launch, Profile


class CommandEngine(Engine):
    plugin = "command"

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        self.metrics_path = cfg.get("metrics_path")
        self.startup_timeout_s = int(cfg.get("startup_timeout_s", 300))

    def launch(self, profile: Profile) -> Launch:
        c = profile.cfg
        return Launch(cmd=[str(x) for x in c["cmd"]], cwd=c.get("cwd"), env={k: str(v) for k, v in (c.get("env") or {}).items()})

    def health(self, profile):
        path = self.cfg.get("health_path", "/")
        try:
            r = httpx.get(self.base_url() + path, headers=self.auth_headers(), timeout=5)
        except httpx.HTTPError as e:
            return False, f"not answering ({type(e).__name__})"
        return r.status_code < 500, f"HTTP {r.status_code}"
