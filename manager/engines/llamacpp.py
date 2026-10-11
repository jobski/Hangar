"""llama.cpp's llama-server.

Profiles name a model (and optionally an mmproj); the plugin adds host, port, API key and --metrics itself.
Updates come from the official GitHub releases: the new build is unpacked next to the current folder, checked, and
swapped in; the old folder is kept as <dir>.prev for rollback.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import httpx

from .base import Engine, Estimate, Launch, Profile, arg_value, file_mib

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
GH = "https://api.github.com/repos/{repo}"
RUNTIME_DLL = re.compile(r"^(cudart|cublas|cublasLt)64_\d+\.dll$", re.I)
BINARY = re.compile(r"\.(exe|dll)$", re.I)


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p.strip('"')))


class LlamaCppEngine(Engine):
    plugin = "llamacpp"
    can_update = True
    can_rollback = True
    metrics_path = "/metrics"

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        self.dir = Path(cfg["dir"])
        self.exe = self.dir / cfg.get("exe", "llama-server.exe")
        self.startup_timeout_s = int(cfg.get("startup_timeout_s", 900))
        u = cfg.get("update") or {}
        self.repo = u.get("repo", "ggml-org/llama.cpp")
        self.channel = u.get("channel", "builds")            # builds = every bNNNN build, stable = releases/latest
        self.asset_re = re.compile(u.get("asset", r"^llama-b\d+-bin-win-cuda-13\.\d+-x64\.zip$"))
        self.runtime_re = re.compile(u.get("runtime_asset", r"^cudart-llama-bin-win-cuda-13\.\d+-x64\.zip$"))
        self.downloads = Path(cfg.get("_data_dir", ".")) / "downloads" / name

    # ---- running
    def launch(self, profile: Profile) -> Launch:
        c = profile.cfg
        args = [str(a) for a in c.get("args", [])]
        cmd = [str(self.exe), "-m", str(c["model"])]
        if c.get("alias"):
            cmd += ["--alias", str(c["alias"])]
        if c.get("mmproj"):
            cmd += ["--mmproj", str(c["mmproj"])]
        if c.get("ctx"):
            cmd += ["-c", str(c["ctx"])]
        cmd += ["--host", self.host, "--port", str(self.port)]
        if "--metrics" not in args:
            cmd.append("--metrics")
        if self.api_key:
            cmd += ["--api-key", self.api_key]
        return Launch(cmd=cmd + args, cwd=str(self.dir), env={k: str(v) for k, v in (c.get("env") or {}).items()})

    def health(self, profile):
        try:
            r = httpx.get(self.base_url() + "/health", headers=self.auth_headers(), timeout=5)
        except httpx.HTTPError as e:
            return False, f"not answering ({type(e).__name__})"
        if r.status_code == 503:
            return False, "loading the model"
        return r.status_code == 200, f"HTTP {r.status_code}"

    def match_process(self, cmdline, profiles):
        model = arg_value(cmdline, "-m", "--model")
        if not model:
            return None
        for p in profiles.values():
            if p.engine == self.name and p.cfg.get("model") and _norm(str(p.cfg["model"])) == _norm(model):
                return p.id
        return None

    def estimate(self, profile, vram_total_mib, ram_total_mib):
        r = profile.cfg.get("requires") or {}
        if r:
            return super().estimate(profile, vram_total_mib, ram_total_mib)
        c = profile.cfg
        args = [str(a) for a in c.get("args", [])]
        files = file_mib(c.get("model"), c.get("mmproj"), arg_value(args, "-md", "--model-draft"))
        ctx = int(c.get("ctx") or arg_value(args, "-c", "--ctx-size") or 4096)
        kv = ctx / 1024 * 40                                 # rough: ~40 MiB per 1K tokens of q8 KV cache on a 35B MoE
        ngl = arg_value(args, "-ngl", "--n-gpu-layers", "--gpu-layers")
        greedy = ngl in (None, "auto", "all") and ("--fit" in args or ngl in ("auto", "all"))
        if ngl == "0":
            return Estimate(500, files + kv, False, "CPU only (-ngl 0)")
        vram = min(files + kv, max(0.0, vram_total_mib)) if greedy or ngl in ("99", "999") else min(files, vram_total_mib)
        ram = max(0.0, files + kv - vram) + 1024
        return Estimate(vram, ram, greedy, "estimated from file sizes and context; replaced by measured use after a run")

    # ---- updates
    def version(self) -> str | None:
        b = self._installed_build(self.exe)
        return f"b{b}" if b else None

    @staticmethod
    def _installed_build(exe: Path) -> int | None:
        if not exe.exists():
            return None
        try:
            out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=30,
                                 creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return None
        m = re.search(r"build (\d+)", out.stdout + out.stderr)
        return int(m.group(1)) if m else None

    def _gh(self, path: str):
        headers = {"Accept": "application/vnd.github+json"}
        if os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
        r = httpx.get(GH.format(repo=self.repo) + path, headers=headers, timeout=30, follow_redirects=True)
        r.raise_for_status()
        return r.json()

    def _latest_release(self) -> dict:
        if self.channel == "stable":
            rels = [self._gh("/releases/latest")]
        else:
            rels = self._gh("/releases?per_page=10")
        for rel in rels:
            if any(self.asset_re.match(a["name"]) for a in rel.get("assets", [])):
                return rel
        raise RuntimeError(f"no release in {self.repo} has an asset matching {self.asset_re.pattern}")

    @staticmethod
    def _build_of(tag: str) -> int | None:
        m = re.fullmatch(r"b(\d+)", tag or "")
        return int(m.group(1)) if m else None

    def check_update(self, log):
        cur = self._installed_build(self.exe)
        rel = self._latest_release()
        latest = self._build_of(rel["tag_name"])
        if self.channel == "stable" and latest is None:
            asset = next(a for a in rel["assets"] if self.asset_re.match(a["name"]))
            m = re.search(r"-b(\d+)-", asset["name"])
            latest = int(m.group(1)) if m else None
        available = bool(latest and (cur is None or latest > cur))
        log(f"{self.name}: installed b{cur}, newest {rel['tag_name']} (b{latest})"
            + (" - update available" if available else " - up to date"))
        return {"current": f"b{cur}" if cur else None, "latest": f"b{latest}" if latest else rel["tag_name"],
                "available": available, "published": rel.get("published_at")}

    def _download(self, asset: dict, log) -> Path:
        self.downloads.mkdir(parents=True, exist_ok=True)
        dest = self.downloads / asset["name"]
        if dest.exists() and dest.stat().st_size == asset["size"]:
            log(f"  already downloaded: {asset['name']}")
            return dest
        log(f"  downloading {asset['name']} ({asset['size'] / 2**20:.0f} MiB)")
        tmp = dest.with_suffix(".part")
        with httpx.stream("GET", asset["browser_download_url"], follow_redirects=True, timeout=120) as r:
            r.raise_for_status()
            done, last = 0, time.time()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if time.time() - last > 10:
                        log(f"    {done / 2**20:.0f} / {asset['size'] / 2**20:.0f} MiB")
                        last = time.time()
        tmp.replace(dest)
        return dest

    @staticmethod
    def _extract(zf: Path, into: Path):
        with zipfile.ZipFile(zf) as z:
            names = z.namelist()
            tops = {n.split("/", 1)[0] for n in names}
            strip = len(tops) == 1 and all("/" in n for n in names)   # everything under one top folder: flatten it
            for info in z.infolist():
                name = info.filename.split("/", 1)[1] if strip else info.filename
                if not name or name.endswith("/"):
                    continue
                target = into / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    def update(self, log):
        info = self.check_update(log)
        if not info["available"]:
            return {**info, "updated": False}
        rel = self._latest_release()
        asset = next(a for a in rel["assets"] if self.asset_re.match(a["name"]))
        runtime = next((a for a in rel["assets"] if self.runtime_re.match(a["name"])), None)
        new, prev = self.dir.with_name(self.dir.name + ".new"), self.dir.with_name(self.dir.name + ".prev")
        if new.exists():
            shutil.rmtree(new)
        new.mkdir(parents=True)
        self._extract(self._download(asset, log), new)

        # the CUDA runtime DLLs ship in a separate zip that rarely changes: reuse ours unless the zip is a new one
        marker = self.dir / ".runtime-asset.json"
        have = json.loads(marker.read_text()) if marker.exists() else None
        want = {"name": runtime["name"], "size": runtime["size"]} if runtime else None
        # no marker yet (installed by hand): fetch the runtime once, since the DLLs there may be an older CUDA minor
        if want and want != have:
            self._extract(self._download(runtime, log), new)
        else:
            for p in self.dir.iterdir():
                if p.is_file() and RUNTIME_DLL.match(p.name) and not (new / p.name).exists():
                    shutil.copy2(p, new / p.name)
        if want:
            (new / marker.name).write_text(json.dumps(want))
        # files the user put in the folder (scripts, notes) come along; binaries never do
        for p in self.dir.iterdir():
            if p.is_file() and not BINARY.search(p.name) and not (new / p.name).exists():
                shutil.copy2(p, new / p.name)

        built = self._installed_build(new / self.exe.name)
        if not built:
            shutil.rmtree(new, ignore_errors=True)
            raise RuntimeError("the new llama-server did not start (--version failed); kept the current one")
        log(f"  new build runs: b{built}")
        if prev.exists():
            shutil.rmtree(prev)
        self.dir.rename(prev)
        new.rename(self.dir)
        log(f"{self.name}: updated to b{built}; the previous build is in {prev}")
        return {**info, "updated": True, "current": f"b{built}"}

    def rollback(self, log):
        prev = self.dir.with_name(self.dir.name + ".prev")
        if not prev.exists():
            raise RuntimeError(f"nothing to roll back to: {prev} does not exist")
        tmp = self.dir.with_name(self.dir.name + ".rollback-tmp")
        self.dir.rename(tmp)
        prev.rename(self.dir)
        tmp.rename(prev)
        log(f"{self.name}: rolled back to {self.version()} (the newer build is now in {prev})")
        return {"current": self.version()}
