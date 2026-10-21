"""The model library (one folder per model under models.dir) and profiles managed from the UI (profiles.yaml).

config.yaml stays hand-written; profiles created, copied or edited from the web UI/API/MCP go to profiles.yaml next to
it, and are merged in (config.yaml wins on a name clash).
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import threading
from pathlib import Path

import yaml

from .config import ROOT

SHARD = re.compile(r"^(?P<stem>.+)-(?P<i>\d{5})-of-(?P<n>\d{5})\.gguf$", re.I)
profiles_lock = threading.RLock()


def profiles_path() -> Path:
    return Path(os.environ.get("LLM_MANAGER_PROFILES") or ROOT / "profiles.yaml")


def load_managed() -> dict:
    p = profiles_path()
    if not p.exists():
        return {}
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("profiles") or {}


def save_managed(profiles: dict):
    with profiles_lock:
        p = profiles_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text("# Profiles made from the web UI / API. Edit freely; config.yaml profiles win on a name clash.\n"
                       + yaml.safe_dump({"profiles": profiles}, sort_keys=False, allow_unicode=True), encoding="utf-8")
        tmp.replace(p)


@contextlib.contextmanager
def edit_managed():
    """Keep a managed-profile read/change/write transaction under one lock."""
    with profiles_lock:
        profiles = load_managed()
        yield profiles
        save_managed(profiles)


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", s.lower()).strip("-.")
    return re.sub(r"-{2,}", "-", s) or "model"


def folder_name_for(repo_or_name: str) -> str:
    """peculiar-ragdoll/Occult-Nail-1.0-35B-A3B-GGUF -> occult-nail-1.0-35b-a3b"""
    base = repo_or_name.rstrip("/").split("/")[-1]
    base = re.sub(r"[-_.]?gguf$", "", base, flags=re.I)
    return slug(base)


def _gguf_groups(folder: Path) -> list[dict]:
    """GGUF files in a folder, split shards grouped; each group says whether it is a vision projector."""
    groups: dict[str, dict] = {}
    for f in sorted(folder.glob("*.gguf")):
        m = SHARD.match(f.name)
        key = m.group("stem") if m else f.stem
        g = groups.setdefault(key, {"name": key, "files": [], "size": 0, "first": None})
        g["files"].append(f.name)
        g["size"] += f.stat().st_size
        if not m or int(m.group("i")) == 1:
            g["first"] = str(f)
    out = []
    for g in groups.values():
        g["mmproj"] = "mmproj" in g["name"].lower() or "projector" in g["name"].lower()
        g["complete"] = _complete(g)
        out.append(g)
    return out


def _complete(g: dict) -> bool:
    m = SHARD.match(g["files"][0]) if g["files"] else None
    return not m or len(g["files"]) == int(m.group("n"))


class Library:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        mc = cfg.get("models") or {}
        self.dir = Path(mc.get("dir") or "E:/models")
        self.scan_dirs = [Path(os.path.expandvars(os.path.expanduser(d))) for d in mc.get("scan", [])]

    def _references(self, p, strict: bool = False) -> list[Path]:
        ecfg = (self.cfg.get("engines") or {}).get(p.engine, {})
        plugin = ecfg.get("plugin", p.engine)
        cwd = ecfg.get("dir") if plugin in ("llamacpp", "strata", "maya") else p.cfg.get("cwd")
        base = Path(cwd) if cwd else Path.cwd()
        out = []
        values = [p.cfg.get(key) for key in ("model", "mmproj")]
        args = list(p.cfg.get("args") or [])
        if plugin in ("strata", "maya") and p.cfg.get("config"):
            from .config import read_json
            try:
                rc = read_json(Path(p.cfg["config"]))
                if not isinstance(rc, dict):
                    raise ValueError("run config must be an object")
                args += rc.get("args") or []
                if plugin == "maya":
                    values += [(rc.get("vision") or {}).get(key) for key in ("model", "mmproj")]
            except (OSError, ValueError, TypeError):
                if strict:
                    raise ValueError(f"cannot verify model references for {p.id}; repair its run config first")
        from .engines.base import arg_value
        for names in (("-m", "--model"), ("--mmproj",), ("-md", "--model-draft"), ("--native",), ("--pack",), ("--glm-pack",)):
            values.append(arg_value([str(a) for a in args], *names))
        for value in values:
            if value:
                path = Path(os.path.expanduser(os.path.expandvars(str(value).strip('"'))))
                path = path if path.is_absolute() else base / path
                out.extend((Path(os.path.abspath(path)), path.resolve()))
                if plugin == "maya" and path.is_dir():
                    from .engines.maya import pack_sources
                    out.extend(source.resolve() for source in pack_sources(path))
        return out

    def list(self, profiles: dict | None = None) -> list[dict]:
        used: dict[str, list[str]] = {}
        for p in (profiles or {}).values():
            for path in self._references(p):
                used.setdefault(os.path.normcase(str(path)), []).append(p.id)
        out = []
        roots = [(self.dir, "library")] + [(d, "external") for d in self.scan_dirs]
        for root, kind in roots:
            if not root.exists():
                continue
            folders = []
            for folder, dirs, _ in os.walk(root):
                folder = Path(folder)
                if len(folder.relative_to(root).parts) >= 3:
                    dirs[:] = []
                folders.append(folder)
            for folder in folders:
                groups = _gguf_groups(folder)
                if not groups:
                    continue
                for g in groups:
                    g["used_by"] = sorted({pid for name in g["files"]
                                           for pid in used.get(os.path.normcase(str((folder / name).resolve())), [])})
                notes = folder / "notes.md"
                out.append({"folder": str(folder), "name": folder.name if folder != root else root.name, "kind": kind,
                            "models": [g for g in groups if not g["mmproj"]],
                            "mmproj": [g for g in groups if g["mmproj"]],
                            "size": sum(g["size"] for g in groups),
                            "partial": [p.name for p in folder.glob("*.part")],
                            "notes": notes.read_text(encoding="utf-8")[:2000] if notes.exists() else ""})
        return out

    def delete(self, folder: str, profiles: dict):
        f = Path(folder).resolve()
        if self.dir.resolve() not in f.parents:
            raise ValueError("only folders inside the model library can be deleted from here")
        users = sorted({p.id for p in profiles.values()
                        if any(path == f or f in path.parents for path in self._references(p, strict=True))})
        if users:
            raise ValueError(f"profiles still use this model: {', '.join(users)}; delete them first")
        shutil.rmtree(f)


CONTEXTS = (262144, 131072, 98304, 65536, 49152, 32768, 24576, 16384, 12288, 8192, 4096)
OVERHEAD_MIB = 1024          # compute buffers and the server itself, on top of weights and KV cache


def fit_context(model: Path, mmproj: str | None, args: list[str], limit_mib: float, want: int) -> tuple[int, str]:
    """The largest context up to `want` (and the model's own maximum) whose weights + KV cache + overhead stay within
    limit_mib; 4096 with a warning when not even that fits."""
    from . import gguf
    from .engines.llamacpp import kv_mib
    shards = sorted(model.parent.glob(SHARD.match(model.name).group("stem") + "-*-of-*.gguf")) if SHARD.match(model.name) else [model]
    weights = sum(f.stat().st_size for f in shards if f.exists()) / 2**20 + (Path(mmproj).stat().st_size / 2**20 if mmproj and Path(mmproj).exists() else 0)
    top = min(want, gguf.max_context(gguf.metadata(model)) or want)
    for c in [top] + [c for c in CONTEXTS if c < top]:
        total = weights + kv_mib(str(model), args, c) + OVERHEAD_MIB
        if total <= limit_mib:
            return c, f"{c // 1024}K context: ~{total / 1024:.1f} GB with {weights / 1024:.1f} GB of weights, within {limit_mib / 1024:.1f} GB"
    total = weights + kv_mib(str(model), args, 4096) + OVERHEAD_MIB
    return 4096, f"needs ~{total / 1024:.1f} GB even at 4K context: more than the {limit_mib / 1024:.1f} GB allowed"


def profile_from_model(model_path: str, cfg: dict, name: str | None = None, engine: str = "llamacpp",
                       ctx: int | None = None, mmproj: str | None = None, extra: dict | None = None,
                       limit_mib: float | None = None) -> tuple[str, dict]:
    """A new llama.cpp-style profile for a GGUF, using the engine's template from config (templates.<engine>). With
    limit_mib (the memory budget, or what the GPU may use on a Mac) and no explicit ctx, the context is the largest
    that fits."""
    mp = Path(model_path)
    if not mp.exists():
        raise ValueError(f"{mp} does not exist")
    shard = SHARD.match(mp.name)
    if shard:
        expected = {f"{shard.group('stem')}-{i:05d}-of-{int(shard.group('n')):05d}.gguf"
                    for i in range(1, int(shard.group("n")) + 1)}
        if not expected.issubset({p.name for p in mp.parent.iterdir()}):
            raise ValueError("all model shards must finish downloading before creating a profile")
    tmpl = dict((cfg.get("templates") or {}).get(engine) or {})
    if mmproj is None:
        cands = [g for g in _gguf_groups(mp.parent) if g["mmproj"]]
        mmproj = cands[0]["first"] if len(cands) >= 1 else None
    stem = SHARD.match(mp.name).group("stem") if SHARD.match(mp.name) else mp.stem
    pid = slug(name or stem)
    prof = {"engine": engine, "description": stem, "policy": tmpl.pop("policy", "exclusive"), "model": str(mp)}
    if mmproj:
        prof["mmproj"] = str(mmproj)
    prof["alias"] = tmpl.pop("alias", None) or pid
    want = int(ctx or tmpl.pop("ctx", 32768))
    tmpl.pop("ctx", None)
    prof["args"] = [str(a) for a in tmpl.pop("args", [])]
    prof["ctx"] = want
    if limit_mib and not ctx:
        prof["ctx"], note = fit_context(mp, prof.get("mmproj"), prof["args"], limit_mib, want)
        prof["description"] = f"{stem} · {note}"
    prof.update(tmpl)
    prof.update(extra or {})
    return pid, prof
