"""Model downloads: Hugging Face, ModelScope, the Ollama library, or any URL - into the model library.

Downloads run on their own threads (not the engine job queue), resume from a .part file after an interruption, and
use aria2c with parallel connections when it is installed and enabled.

Each download leaves a record (.hangar-download.json) in its folder until it completes, so one that was cut off - a
restart, a crash, a reboot - is found again later and can be resumed or discarded (leftovers()).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import httpx

from .config import write_json
from .models import SHARD, folder_name_for

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
QUANT = re.compile(r"(?:^|[-_.])((?:UD-)?(?:I?Q\d(?:_[A-Z0-9]+)*|BF16|F16|F32|MXFP4(?:_MOE)?))(?:[-_.]|$)", re.I)
UA = {"User-Agent": "hangar/0.1"}


def hf_token() -> str | None:
    if os.environ.get("HF_TOKEN"):
        return os.environ["HF_TOKEN"]
    home = Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface")
    f = home / "token"
    return f.read_text().strip() if f.exists() else None


# ------------------------------------------------------------------------------------------------- sources
class Source:
    name = ""

    def search(self, q: str, limit: int = 20) -> list[dict]:
        return []

    def files(self, repo: str, revision: str | None = None) -> list[dict]:
        raise NotImplementedError

    def url(self, repo: str, path: str, revision: str | None = None) -> str:
        raise NotImplementedError

    def headers(self) -> dict:
        return dict(UA)


class HuggingFace(Source):
    name = "huggingface"
    base = "https://huggingface.co"

    def headers(self):
        t = hf_token()
        return {**UA, **({"Authorization": f"Bearer {t}"} if t else {})}

    def search(self, q, limit=20):
        r = httpx.get(f"{self.base}/api/models", params={"search": q, "filter": "gguf", "sort": "downloads",
                                                         "direction": "-1", "limit": limit}, headers=self.headers(), timeout=30)
        r.raise_for_status()
        return [{"repo": m["id"], "downloads": m.get("downloads"), "likes": m.get("likes"),
                 "updated": m.get("lastModified")} for m in r.json()]

    def files(self, repo, revision=None):
        r = httpx.get(f"{self.base}/api/models/{repo}/tree/{revision or 'main'}", params={"recursive": "true"},
                      headers=self.headers(), timeout=60, follow_redirects=True)
        if r.status_code in (401, 403):
            raise ValueError(f"{repo} is gated or private: accept its license on huggingface.co and run `hf auth login`")
        r.raise_for_status()
        return [{"path": f["path"], "size": (f.get("lfs") or {}).get("size") or f.get("size", 0),
                 "revision": r.headers.get("x-repo-commit") or revision}
                for f in r.json() if f.get("type") == "file"]

    def url(self, repo, path, revision=None):
        return f"{self.base}/{repo}/resolve/{revision or 'main'}/{quote(path)}"


class ModelScope(Source):
    name = "modelscope"
    base = "https://modelscope.cn"

    def search(self, q, limit=20):
        r = httpx.put(f"{self.base}/api/v1/dolphin/models", json={"PageSize": limit, "PageNumber": 1, "SortBy": "Default",
                      "Target": "", "SingleCriterion": [], "Name": q}, headers=self.headers(), timeout=30)
        r.raise_for_status()
        items = ((r.json().get("Data") or {}).get("Model") or {}).get("Models") or []
        return [{"repo": f"{m.get('Path')}/{m.get('Name')}", "downloads": m.get("Downloads"), "likes": m.get("Stars"),
                 "updated": m.get("LastUpdatedTime")} for m in items]

    def files(self, repo, revision=None):
        r = httpx.get(f"{self.base}/api/v1/models/{repo}/repo/files", params={"Revision": revision or "master",
                      "Recursive": "true"}, headers=self.headers(), timeout=60)
        r.raise_for_status()
        files = ((r.json().get("Data") or {}).get("Files")) or []
        return [{"path": f["Path"], "size": f.get("Size", 0)} for f in files if f.get("Type") == "blob"]

    def url(self, repo, path, revision=None):
        return f"{self.base}/models/{repo}/resolve/{revision or 'master'}/{quote(path)}"


class Ollama(Source):
    """registry.ollama.ai: a model layer is a plain GGUF; a projector layer is its mmproj."""
    name = "ollama"
    base = "https://registry.ollama.ai"
    kinds = {"application/vnd.ollama.image.model": "", "application/vnd.ollama.image.projector": "mmproj-"}

    @staticmethod
    def _split(repo: str):
        name, _, tag = repo.partition(":")
        ns, _, model = name.rpartition("/")
        return ns or "library", model, tag or "latest"

    def files(self, repo, revision=None):
        ns, model, tag = self._split(repo)
        r = httpx.get(f"{self.base}/v2/{ns}/{model}/manifests/{tag}", timeout=30, headers={
            **UA, "Accept": "application/vnd.docker.distribution.manifest.v2+json"})
        if r.status_code == 404:
            raise ValueError(f"ollama has no {ns}/{model}:{tag}")
        r.raise_for_status()
        out = []
        for layer in r.json().get("layers", []):
            prefix = self.kinds.get(layer["mediaType"])
            if prefix is not None:
                out.append({"path": f"{prefix}{model}-{tag}.gguf".replace(":", "-"), "size": layer["size"],
                            "digest": layer["digest"]})
        return out

    def url(self, repo, path, revision=None):
        ns, model, _ = self._split(repo)
        digest = next(f["digest"] for f in self.files(repo) if f["path"] == path)
        return f"{self.base}/v2/{ns}/{model}/blobs/{digest}"


class DirectURL(Source):
    name = "url"

    def files(self, repo, revision=None):
        name = unquote(Path(urlparse(repo).path).name) or "download.bin"
        size = 0
        try:
            r = httpx.head(repo, follow_redirects=True, timeout=30, headers=self.headers())
            size = int(r.headers.get("content-length") or 0)
            cd = r.headers.get("content-disposition", "")
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
            if m:
                name = unquote(m.group(1))
        except httpx.HTTPError:
            pass
        return [{"path": name, "size": size, "url": repo}]

    def url(self, repo, path, revision=None):
        return repo


SOURCES = {s.name: s for s in (HuggingFace(), ModelScope(), Ollama(), DirectURL())}


def annotate(files: list[dict]) -> dict:
    """Group a repo's GGUFs by quant (shards together) so a UI can offer 'pick a quant'."""
    groups: dict[str, dict] = {}
    for f in files:
        name = Path(f["path"]).name
        if not name.lower().endswith(".gguf"):
            continue
        m = SHARD.match(name)
        stem = m.group("stem") if m else name[:-5]
        key = (str(Path(f["path"]).parent) + "/" + stem).lstrip("./")
        q = QUANT.search(stem)
        g = groups.setdefault(key, {"key": key, "quant": q.group(1).upper() if q else None, "files": [], "size": 0,
                                    "mmproj": "mmproj" in stem.lower()})
        g["files"].append(f["path"])
        g["size"] += f["size"] or 0
    return {"groups": sorted(groups.values(), key=lambda g: (g["mmproj"], g["size"])), "files": files}


RECORD = ".hangar-download.json"


# ----------------------------------------------------------------------------------------------- downloads
@dataclass
class Download:
    source: str
    repo: str
    files: list[dict]
    folder: str
    revision: str | None = None
    make_profile: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    state: str = "queued"            # queued | running | done | failed | cancelled
    created: float = field(default_factory=time.time)
    finished: float | None = None
    done_bytes: int = 0
    total_bytes: int = 0
    speed: float = 0.0
    current: str = ""
    error: str | None = None
    log: list[str] = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)
    result: dict = field(default_factory=dict)

    def write(self, msg):
        self.log.append(time.strftime("%H:%M:%S ") + msg)
        del self.log[:-300]

    def public(self):
        eta = (self.total_bytes - self.done_bytes) / self.speed if self.speed > 0 and self.state == "running" else None
        return {"id": self.id, "source": self.source, "repo": self.repo, "folder": self.folder, "state": self.state,
                "files": [f["path"] for f in self.files], "done_bytes": self.done_bytes, "total_bytes": self.total_bytes,
                "speed": self.speed, "eta_seconds": eta, "current": self.current, "error": self.error,
                "created": self.created, "finished": self.finished, "log": self.log[-20:], "result": self.result}


class Downloader:
    def __init__(self, cfg: dict, library_dir: Path, on_done=None):
        dc = cfg.get("downloads") or {}
        self.library_dir = library_dir
        self.parallel = threading.Semaphore(int(dc.get("max_parallel", 2)))
        self.aria2 = dc.get("aria2", "auto")             # auto | on | off
        self.aria2_connections = int(dc.get("aria2_connections", 16))
        self.items: dict[str, Download] = {}
        self.items_lock = threading.Lock()               # API threads and download threads both use items
        self.on_done = on_done
        self._leftovers_cache = None
        # folders downloaded into outside the library (e.g. a Strata model folder), so leftovers() finds them too
        self.known_file = Path(cfg["manager"]["data_dir"]) / "download-folders.json" if cfg.get("manager") else None

    def _items(self) -> list[Download]:
        with self.items_lock:
            return list(self.items.values())

    def _known_folders(self) -> list[Path]:
        try:
            return [Path(x) for x in json.loads(self.known_file.read_text(encoding="utf-8"))]
        except (OSError, ValueError, AttributeError, TypeError):
            return []

    def _remember_folder(self, folder: Path):
        if not self.known_file or folder.parent == self.library_dir:
            return
        known = {str(x) for x in self._known_folders()} | {str(folder)}
        try:
            write_json(self.known_file, sorted(known))
        except OSError:
            pass

    def aria2_path(self) -> str | None:
        if self.aria2 == "off":
            return None
        p = shutil.which("aria2c")
        if p:
            return p
        # where installers put it when it is not on this process's PATH (Hangar may run without the shell's PATH)
        for c in ("/opt/homebrew/bin/aria2c", "/usr/local/bin/aria2c", "/usr/bin/aria2c"):
            if Path(c).exists():
                return c
        for c in Path(os.environ.get("LOCALAPPDATA", "")).glob("Microsoft/WinGet/Packages/aria2.aria2*/**/aria2c.exe"):
            return str(c)
        return None

    @staticmethod
    def aria2_hint() -> str:
        """The command that installs aria2 on this machine."""
        if sys.platform == "darwin":
            return "brew install aria2"
        if sys.platform.startswith("linux"):
            return "sudo apt install aria2   (or your distribution's package manager)"
        return "winget install aria2.aria2"

    # ---- browsing
    def search(self, source: str, q: str, limit: int = 20):
        return SOURCES[self._src(source)].search(q, limit)

    def list_files(self, source: str, repo: str, revision: str | None = None):
        return annotate(SOURCES[self._src(source)].files(repo, revision))

    @staticmethod
    def _src(source: str) -> str:
        source = {"hf": "huggingface", "ms": "modelscope"}.get(source, source)
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}: {', '.join(SOURCES)}")
        return source

    # ---- downloading
    def _safe_dest(self, folder: str | None) -> Path:
        root = self.library_dir.resolve()
        dest = (root / (folder or "")).resolve()
        if dest == root or root not in dest.parents:
            raise ValueError("download folders must be inside the model library")
        return dest

    @staticmethod
    def _filename(path: str) -> str:
        name = Path(path).name
        if not name or name.lower() in (".", "..", RECORD, "notes.md") or "\\" in path or ":" in name:
            raise ValueError(f"unsafe download filename: {name!r}")
        return name

    def _invalidate_leftovers(self):
        self._leftovers_cache = None

    def start(self, source: str, repo: str, include: list[str] | None = None, folder: str | None = None,
              revision: str | None = None, make_profile: bool = False, files: list[dict] | None = None) -> Download:
        """files: the exact files (as listed before), e.g. to resume a download from its record."""
        source = self._src(source)
        src = SOURCES[source]
        if not files:
            files = src.files(repo, revision)
            if include:
                pats = [re.compile(_glob_re(p), re.I) for p in include]
                files = [f for f in files if any(p.search(f["path"]) for p in pats)]
            elif source in ("huggingface", "modelscope"):
                raise ValueError("say which files: include=[\"*Q4_K_M*\"] (list_files shows the quants)")
        if not files:
            raise ValueError("no files match")
        dest = self._safe_dest(folder or folder_name_for(
            repo.split(":")[0] if source == "ollama" else Path(urlparse(repo).path).stem if source == "url" else repo))
        names = [self._filename(f["path"]) for f in files]
        if len(names) != len(set(names)):
            raise ValueError("selected files have duplicate filenames")
        d = Download(source=source, repo=repo, files=files, folder=str(dest), revision=revision, make_profile=make_profile)
        d.total_bytes = sum(f.get("size") or 0 for f in files)
        with self.items_lock:                      # the check and the claim together: two clicks start it once
            if any(x.folder == str(dest) and x.state in ("queued", "running") for x in self.items.values()):
                raise ValueError(f"{dest.name} is already downloading")
            self.items[d.id] = d
        try:
            dest.mkdir(parents=True, exist_ok=True)
            self._remember_folder(dest)
            write_json(dest / RECORD, {"source": source, "repo": repo, "revision": revision, "make_profile":
                                       make_profile, "files": files, "started": time.time()})
        except OSError:
            with self.items_lock:                  # not started after all: free the folder for another try
                self.items.pop(d.id, None)
            raise
        threading.Thread(target=self._run, args=(d,), daemon=True, name=f"dl-{d.id}").start()
        self._invalidate_leftovers()
        return d

    def cancel(self, did: str):
        with self.items_lock:
            d = self.items[did]
            d.cancel.set()
            if d.state == "queued":
                d.state, d.finished = "cancelled", time.time()
        self._invalidate_leftovers()
        return d

    def list(self):
        return [d.public() for d in sorted(self._items(), key=lambda x: -x.created)][:50]

    def _run(self, d: Download):
        with self.parallel:
            if d.cancel.is_set():
                d.state = "cancelled"
                return
            d.state = "running"
            src = SOURCES[d.source]
            try:
                dest = self._safe_dest(d.folder)
                dest.mkdir(parents=True, exist_ok=True)
                base_done = 0
                for f in d.files:
                    name = self._filename(f["path"])
                    d.current = name
                    url = f.get("url") or (f"{src.base}/v2/{src._split(d.repo)[0]}/{src._split(d.repo)[1]}/blobs/{f['digest']}"
                                               if d.source == "ollama" and f.get("digest") else src.url(d.repo, f["path"], f.get("revision") or d.revision))
                    target = dest / name
                    if any(dest not in p.resolve().parents for p in (target, target.with_name(name + ".part"))):
                        raise ValueError("download file escapes its destination")
                    if target.exists() and not target.with_name(name + ".aria2").exists() and f.get("size") and target.stat().st_size == f["size"]:
                        d.write(f"already have {name}")
                    else:
                        d.write(f"downloading {name} ({(f.get('size') or 0) / 2**30:.2f} GiB)")
                        aria = self.aria2_path()
                        part = target.with_name(target.name + ".part")
                        if part.exists():
                            aria = None
                        if aria:
                            if target.exists() and not target.with_name(name + ".aria2").exists():
                                raise ValueError(f"unverified existing file {name}; move it before downloading again")
                            old_tag = f.get("etag") or f.get("last_modified")
                            head = httpx.head(url, headers=src.headers(), follow_redirects=True, timeout=30)
                            if head.status_code in (405, 501):
                                if target.exists():
                                    raise ValueError(f"cannot verify the partial {name}; discard it before restarting")
                                aria = None
                            else:
                                head.raise_for_status()
                            new_tag = head.headers.get("etag") or head.headers.get("last-modified")
                            if target.exists() and (not old_tag or old_tag != new_tag):
                                raise ValueError(f"cannot verify the partial {name}; discard it before restarting")
                            f.update(etag=head.headers.get("etag"), last_modified=head.headers.get("last-modified"))
                            self._record(d)
                            try:
                                if aria:
                                    self._aria2(d, aria, url, src.headers(), target, base_done)
                                else:
                                    self._http(d, url, src.headers(), target, base_done, f.get("size") or 0)
                                if f.get("size") and target.stat().st_size != f["size"]:
                                    raise RuntimeError(f"{name}: downloaded size does not match the source")
                            except _AriaFailed as e:
                                ctl = target.with_name(target.name + ".aria2")
                                if ctl.exists():
                                    # aria2 has pieces of the file all over it: only aria2 can continue that
                                    raise RuntimeError(f"{e}; its partial file is kept for aria2 to resume") from None
                                # it never got going (e.g. it could not look the host up): what is on disk is still a
                                # plain from-the-start file, so the built-in downloader carries on from it
                                d.write(f"  {e}; continuing with the built-in downloader")
                                if target.exists() and not part.exists():
                                    target.rename(part)
                                self._http(d, url, src.headers(), target, base_done, f.get("size") or 0)
                        else:
                            self._http(d, url, src.headers(), target, base_done, f.get("size") or 0)
                    base_done += f.get("size") or (target.stat().st_size if target.exists() else 0)
                    d.done_bytes = base_done
                self._notes(d, dest)
                (dest / RECORD).unlink(missing_ok=True)
                d.state, d.current = "done", ""
                d.write(f"done: {dest}")
                if self.on_done:
                    d.result = self.on_done(d) or {}
            except _Cancelled:
                d.state = "cancelled"
                d.write("cancelled (partial files are kept; starting the same download again resumes)")
            except Exception as e:  # noqa: BLE001
                d.state, d.error = "failed", f"{type(e).__name__}: {e}"
                d.write("FAILED: " + d.error)
            finally:
                d.finished = time.time()
                self._invalidate_leftovers()

    # ---- downloads that stopped without finishing (and are not running now)
    def leftovers(self) -> list[dict]:
        """Unfinished downloads in the library: folders with a download record, .part files, aria2 control files.
        resumable when Hangar knows what was being downloaded (the record)."""
        if self._leftovers_cache and time.monotonic() - self._leftovers_cache[0] < 30:
            return self._leftovers_cache[1]
        busy = {x.folder for x in self._items() if x.state in ("queued", "running")}
        out = []
        folders = sorted(p for p in self.library_dir.iterdir() if p.is_dir()) if self.library_dir.is_dir() else []
        folders += [p for p in self._known_folders() if p.is_dir() and p not in folders]
        for folder in folders:
            try:
                folder = self._safe_dest(str(folder))
            except ValueError:
                continue
            if str(folder) in busy:
                continue
            rec = None
            try:
                rec = json.loads((folder / RECORD).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            files = []
            if rec:
                for f in rec.get("files", []):
                    name, size = self._filename(f["path"]), f.get("size") or 0
                    t, part = folder / name, folder / (name + ".part")
                    have = part.stat().st_size if part.exists() else t.stat().st_size if t.exists() else 0
                    done = t.exists() and not part.exists() and not (folder / (name + ".aria2")).exists() and (not size or have >= size)
                    files.append({"name": name, "have": have, "size": size, "complete": done})
                if all(f["complete"] for f in files):     # everything is there: only the record was left behind
                    (folder / RECORD).unlink(missing_ok=True)
                    continue
            else:
                for p in sorted(folder.iterdir()):
                    if p.suffix == ".part":
                        files.append({"name": p.name[:-5], "have": p.stat().st_size, "size": 0, "complete": False})
                    elif p.suffix == ".aria2" and (folder / p.stem).exists():
                        files.append({"name": p.stem, "have": (folder / p.stem).stat().st_size, "size": 0, "complete": False})
                if not files:
                    continue
            mtime = max([p.stat().st_mtime for p in folder.iterdir()] or [0])
            out.append({"folder": str(folder), "name": folder.name, "source": (rec or {}).get("source"),
                        "repo": (rec or {}).get("repo"), "resumable": bool(rec), "files": files,
                        "have_bytes": sum(f["have"] for f in files), "total_bytes": sum(f["size"] for f in files),
                        "last_activity": mtime})
        self._leftovers_cache = (time.monotonic(), out)
        return out

    def _leftover(self, folder: str) -> dict:
        self._invalidate_leftovers()
        lo = next((x for x in self.leftovers() if x["folder"] == folder or x["name"] == folder), None)
        if not lo:
            raise KeyError(f"no unfinished download in {folder}")
        return lo

    def resume(self, folder: str) -> Download:
        lo = self._leftover(folder)
        if not lo["resumable"]:
            raise ValueError(f"{lo['name']} has partial files but no record of what was being downloaded: start the same "
                             "download again from the Models page and it continues from them")
        rec = json.loads((Path(lo["folder"]) / RECORD).read_text(encoding="utf-8"))
        return self.start(rec["source"], rec["repo"], folder=lo["folder"], revision=rec.get("revision"),
                          make_profile=rec.get("make_profile", False), files=rec["files"])

    def discard(self, folder: str) -> dict:
        """Delete what an unfinished download left: partial files and the record; finished files stay. A folder left
        empty (or with only its notes) goes too."""
        lo = self._leftover(folder)
        f_dir = self._safe_dest(lo["folder"])
        removed = []
        for f in lo["files"]:
            if f["complete"]:
                continue
            for p in (f_dir / (f["name"] + ".part"), f_dir / (f["name"] + ".aria2"), f_dir / f["name"]):
                if p.exists():
                    p.unlink()
                    removed.append(p.name)
        (f_dir / RECORD).unlink(missing_ok=True)
        if f_dir.is_dir() and all(p.name == "notes.md" for p in f_dir.iterdir()):
            shutil.rmtree(f_dir)
            removed.append(f_dir.name + "/")
        self._invalidate_leftovers()
        return {"folder": str(f_dir), "removed": removed}

    @staticmethod
    def _record(d: Download):
        write_json(Path(d.folder) / RECORD, {"source": d.source, "repo": d.repo, "revision": d.revision,
                   "make_profile": d.make_profile, "files": d.files, "started": d.created})

    def _http(self, d: Download, url: str, headers: dict, target: Path, base_done: int, size: int):
        part = target.with_name(target.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        h = dict(headers)
        info = next((f for f in getattr(d, "files", []) if Path(f["path"]).name == target.name), {})
        tag = info.get("etag") or info.get("last_modified")
        if have and not tag:
            have = 0
        if have:
            h["If-Range"] = tag
            h["Range"] = f"bytes={have}-"
            d.write(f"  resuming at {have / 2**30:.2f} GiB")
        t0, n0 = time.time(), have
        with httpx.stream("GET", url, headers=h, follow_redirects=True, timeout=httpx.Timeout(60, read=300)) as r:
            if r.status_code == 416:            # already complete
                remote = re.fullmatch(r"bytes \*/(\d+)", r.headers.get("content-range", ""))
                if not remote or not have or have != int(remote.group(1)) or (size and have != size):
                    raise ValueError("partial download does not match the remote size; restart it")
            else:
                if r.status_code == 200 and have:   # server ignored Range: start over
                    have = 0
                r.raise_for_status()
                if r.status_code == 206:
                    span = re.match(r"bytes (\d+)-", r.headers.get("content-range", ""))
                    current_tag = r.headers.get("etag") or r.headers.get("last-modified")
                    if not span or int(span.group(1)) != have or (have and current_tag and current_tag != tag):
                        raise ValueError("server returned an inconsistent download range")
                if info:
                    info.update(etag=r.headers.get("etag"), last_modified=r.headers.get("last-modified"))
                    self._record(d)
                with open(part, "ab" if have else "wb") as out:
                    for chunk in r.iter_bytes(4 << 20):
                        if d.cancel.is_set():
                            raise _Cancelled()
                        out.write(chunk)
                        have += len(chunk)
                        d.done_bytes = base_done + have
                        dt = time.time() - t0
                        if dt > 2:
                            d.speed = (have - n0) / dt
        if size and part.stat().st_size != size:
            raise RuntimeError(f"{target.name}: got {part.stat().st_size} bytes, expected {size}")
        part.replace(target)

    def _aria2(self, d: Download, exe: str, url: str, headers: dict, target: Path, base_done: int):
        cmd = [exe, "--continue=true", f"--max-connection-per-server={self.aria2_connections}",
               f"--split={self.aria2_connections}", "--min-split-size=8M", "--file-allocation=none",
               "--summary-interval=2", "--console-log-level=warn", "--download-result=hide", "--auto-file-renaming=false",
               "--allow-overwrite=true", "-d", str(target.parent), "-o", target.name,
               # CDNs publish IPv6 addresses, and aria2 does not fall back to IPv4 the way other clients do: on a
               # machine without an IPv6 route every connection fails ("unreachable network")
               "--disable-ipv6=true",
               # aria2's own resolver (c-ares) misses macOS DNS - scoped resolvers, Tailscale MagicDNS - and fails
               # with exit 19 ("name resolution failed"): use the system's, as every other program does
               "--async-dns=false"]
        if any("\n" in str(value) or "\r" in str(value) for value in [url, *headers.keys(), *headers.values()]):
            raise ValueError("invalid download URL or headers")
        fd, input_path = tempfile.mkstemp(prefix="hangar-aria2-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as source:
            source.write(url + "\n" + "".join(f"  header={key}: {value}\n" for key, value in headers.items()))
        cmd.append(f"--input-file={input_path}")
        prog = re.compile(r"([\d.]+)([KMG]?i?B)/([\d.]+)([KMG]?i?B).*?DL:([\d.]+)([KMG]?i?B)")
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                                 creationflags=NO_WINDOW)
        except BaseException:
            Path(input_path).unlink(missing_ok=True)
            raise
        finished = threading.Event()

        def watch_cancel():
            while not finished.wait(0.1):
                if d.cancel.is_set():
                    if p.poll() is None:
                        try:
                            p.kill()
                        except OSError:
                            pass
                    return
        watcher = threading.Thread(target=watch_cancel, name="aria2-cancel", daemon=True)
        watcher.start()
        try:
            for line in p.stdout:
                m = prog.search(line)
                if m:
                    d.done_bytes = base_done + _bytes(m.group(1), m.group(2))
                    d.speed = _bytes(m.group(5), m.group(6))
                elif line.strip() and "[#" not in line:
                    d.write("  aria2: " + line.strip()[:200])
            p.wait()
        finally:
            finished.set()
            watcher.join()
            if p.poll() is None:
                p.kill()
            p.wait()
            p.stdout.close()
            Path(input_path).unlink(missing_ok=True)
        if d.cancel.is_set():
            raise _Cancelled()
        if p.returncode != 0:
            raise _AriaFailed(f"aria2c failed: {ARIA2_EXIT.get(p.returncode, 'error')} (exit {p.returncode})")

    @staticmethod
    def _notes(d: Download, dest: Path):
        notes = dest / "notes.md"
        if notes.is_symlink():
            raise ValueError("download notes must not be a symbolic link")
        src = {"huggingface": f"https://huggingface.co/{d.repo}", "modelscope": f"https://modelscope.cn/models/{d.repo}",
               "ollama": f"https://ollama.com/library/{d.repo}", "url": d.repo}[d.source]
        line = f"- {time.strftime('%Y-%m-%d')}: {', '.join(Path(f['path']).name for f in d.files)} from {src}\n"
        if not notes.exists():
            notes.write_text(f"# {dest.name}\n\nSource: {src}\n\n## Downloads\n", encoding="utf-8")
        with open(notes, "a", encoding="utf-8") as f:
            f.write(line)


class _Cancelled(Exception):
    pass


class _AriaFailed(Exception):
    pass


# aria2c's exit codes (man aria2c, "EXIT STATUS"), for messages people can act on
ARIA2_EXIT = {1: "unknown error", 2: "timed out", 3: "file not found on the server", 5: "too slow", 6: "network problem",
              7: "unfinished downloads", 8: "the server does not support resuming", 9: "not enough disk space",
              13: "the file already exists", 14: "could not rename the file", 15: "could not open the existing file",
              16: "could not create the file", 17: "file write error", 18: "could not create the folder",
              19: "name resolution failed (DNS)", 22: "bad HTTP response", 23: "too many redirects",
              24: "authorization failed (gated repo? log in to Hugging Face)", 28: "bad option", 32: "checksum mismatch"}


def _bytes(v: str, unit: str) -> int:
    mult = {"B": 1, "KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "KB": 1e3, "MB": 1e6, "GB": 1e9}.get(unit, 1)
    return int(float(v) * mult)


def _glob_re(pat: str) -> str:
    """'*Q4_K_M*' style patterns (or a plain substring) as a regex on the file path."""
    if any(c in pat for c in "*?"):
        return "^" + re.escape(pat).replace(r"\*", ".*").replace(r"\?", ".") + "$"
    return re.escape(pat)
