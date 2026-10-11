"""The model library (one folder per model under models.dir) and profiles managed from the UI (profiles.yaml).

config.yaml stays hand-written; profiles created, copied or edited from the web UI/API/MCP go to profiles.yaml next to
it, and are merged in (config.yaml wins on a name clash).
"""
from __future__ import annotations

import os
import re
import shutil
import threading
from pathlib import Path

import yaml

from .config import ROOT

SHARD = re.compile(r"^(?P<stem>.+)-(?P<i>\d{5})-of-(?P<n>\d{5})\.gguf$", re.I)
_lock = threading.Lock()


def profiles_path() -> Path:
    return Path(os.environ.get("LLM_MANAGER_PROFILES") or ROOT / "profiles.yaml")


def load_managed() -> dict:
    p = profiles_path()
    if not p.exists():
        return {}
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("profiles") or {}


def save_managed(profiles: dict):
    with _lock:
        p = profiles_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text("# Profiles made from the web UI / API. Edit freely; config.yaml profiles win on a name clash.\n"
                       + yaml.safe_dump({"profiles": profiles}, sort_keys=False, allow_unicode=True), encoding="utf-8")
        tmp.replace(p)


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
        mc = cfg.get("models") or {}
        self.dir = Path(mc.get("dir") or "E:/models")
        self.scan_dirs = [Path(os.path.expandvars(os.path.expanduser(d))) for d in mc.get("scan", [])]

    def list(self, profiles: dict | None = None) -> list[dict]:
        used: dict[str, list[str]] = {}
        for p in (profiles or {}).values():
            for k in ("model", "mmproj"):
                v = p.cfg.get(k)
                if v:
                    used.setdefault(os.path.normcase(os.path.normpath(str(v))), []).append(p.id)
        out = []
        roots = [(self.dir, "library")] + [(d, "external") for d in self.scan_dirs]
        for root, kind in roots:
            if not root.exists():
                continue
            folders = [root] + [p for p in root.rglob("*") if p.is_dir() and len(p.relative_to(root).parts) <= 3]
            for folder in folders:
                groups = _gguf_groups(folder)
                if not groups:
                    continue
                for g in groups:
                    g["used_by"] = used.get(os.path.normcase(os.path.normpath(g["first"] or "")), [])
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
        users = [g["used_by"] for m in self.list(profiles) if Path(m["folder"]).resolve() == f
                 for g in m["models"] + m["mmproj"] if g["used_by"]]
        if users:
            raise ValueError(f"profiles still use this model: {', '.join(sum(users, []))}; delete them first")
        shutil.rmtree(f)


def profile_from_model(model_path: str, cfg: dict, name: str | None = None, engine: str = "llamacpp",
                       ctx: int | None = None, mmproj: str | None = None, extra: dict | None = None) -> tuple[str, dict]:
    """A new llama.cpp-style profile for a GGUF, using the engine's template from config (templates.<engine>)."""
    mp = Path(model_path)
    if not mp.exists():
        raise ValueError(f"{mp} does not exist")
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
    prof["ctx"] = int(ctx or tmpl.pop("ctx", 32768))
    tmpl.pop("ctx", None)
    prof["args"] = [str(a) for a in tmpl.pop("args", [])]
    prof.update(tmpl)
    prof.update(extra or {})
    return pid, prof
