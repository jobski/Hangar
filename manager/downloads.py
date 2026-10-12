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
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import httpx

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
        return [{"path": f["path"], "size": (f.get("lfs") or {}).get("size") or f.get("size", 0)}
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
        self.on_done = on_done
        # folders downloaded into outside the library (e.g. a Strata model folder), so leftovers() finds them too
        self.known_file = Path(cfg["manager"]["data_dir"]) / "download-folders.json" if cfg.get("manager") else None

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
            self.known_file.write_text(json.dumps(sorted(known), indent=1), encoding="utf-8")
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
        dest = Path(folder) if folder and Path(folder).is_absolute() else self.library_dir / (folder or folder_name_for(
            repo.split(":")[0] if source == "ollama" else Path(urlparse(repo).path).stem if source == "url" else repo))
        if any(x.folder == str(dest) and x.state in ("queued", "running") for x in self.items.values()):
            raise ValueError(f"{dest.name} is already downloading")
        d = Download(source=source, repo=repo, files=files, folder=str(dest), revision=revision, make_profile=make_profile)
        d.total_bytes = sum(f.get("size") or 0 for f in files)
        dest.mkdir(parents=True, exist_ok=True)
        self._remember_folder(dest)
        (dest / RECORD).write_text(json.dumps({"source": source, "repo": repo, "revision": revision, "make_profile": make_profile,
                                               "files": files, "started": time.time()}, indent=1), encoding="utf-8")
        self.items[d.id] = d
        threading.Thread(target=self._run, args=(d,), daemon=True, name=f"dl-{d.id}").start()
        return d

    def cancel(self, did: str):
        d = self.items[did]
        d.cancel.set()
        return d

    def list(self):
        return [d.public() for d in sorted(self.items.values(), key=lambda x: -x.created)][:50]

    def _run(self, d: Download):
        with self.parallel:
            if d.cancel.is_set():
                d.state = "cancelled"
                return
            d.state = "running"
            src = SOURCES[d.source]
            dest = Path(d.folder)
            dest.mkdir(parents=True, exist_ok=True)
            try:
                base_done = 0
                for f in d.files:
                    name = Path(f["path"]).name
                    d.current = name
                    url = f.get("url") or src.url(d.repo, f["path"], d.revision)
                    target = dest / name
                    if target.exists() and f.get("size") and target.stat().st_size == f["size"]:
                        d.write(f"already have {name}")
                    else:
                        d.write(f"downloading {name} ({(f.get('size') or 0) / 2**30:.2f} GiB)")
                        aria = self.aria2_path()
                        part = target.with_name(target.name + ".part")
                        if aria and part.exists() and not target.exists():
                            # started without aria2: hand it what is already here (it continues an existing file)
                            part.rename(target)
                            d.write(f"  continuing with aria2 from {target.stat().st_size / 2**30:.2f} GiB")
                        if aria:
                            self._aria2(d, aria, url, src.headers(), target, base_done)
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

    # ---- downloads that stopped without finishing (and are not running now)
    def leftovers(self) -> list[dict]:
        """Unfinished downloads in the library: folders with a download record, .part files, aria2 control files.
        resumable when Hangar knows what was being downloaded (the record)."""
        busy = {x.folder for x in self.items.values() if x.state in ("queued", "running")}
        out = []
        folders = sorted(p for p in self.library_dir.iterdir() if p.is_dir()) if self.library_dir.is_dir() else []
        folders += [p for p in self._known_folders() if p.is_dir() and p not in folders]
        for folder in folders:
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
                    name, size = Path(f["path"]).name, f.get("size") or 0
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
        return out

    def _leftover(self, folder: str) -> dict:
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
        f_dir = Path(lo["folder"])
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
        return {"folder": str(f_dir), "removed": removed}

    def _http(self, d: Download, url: str, headers: dict, target: Path, base_done: int, size: int):
        part = target.with_name(target.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        h = dict(headers)
        if have:
            h["Range"] = f"bytes={have}-"
            d.write(f"  resuming at {have / 2**30:.2f} GiB")
        t0, n0 = time.time(), have
        with httpx.stream("GET", url, headers=h, follow_redirects=True, timeout=httpx.Timeout(60, read=300)) as r:
            if r.status_code == 416:            # already complete
                pass
            else:
                if r.status_code == 200 and have:   # server ignored Range: start over
                    have = 0
                r.raise_for_status()
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
               "--disable-ipv6=true"]
        for k, v in headers.items():
            cmd.append(f"--header={k}: {v}")
        cmd.append(url)
        prog = re.compile(r"([\d.]+)([KMG]?i?B)/([\d.]+)([KMG]?i?B).*?DL:([\d.]+)([KMG]?i?B)")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                             creationflags=NO_WINDOW)
        for line in p.stdout:
            if d.cancel.is_set():
                p.kill()
                raise _Cancelled()
            m = prog.search(line)
            if m:
                d.done_bytes = base_done + _bytes(m.group(1), m.group(2))
                d.speed = _bytes(m.group(5), m.group(6))
            elif line.strip() and "[#" not in line:
                d.write("  aria2: " + line.strip()[:200])
        if p.wait() != 0:
            raise RuntimeError(f"aria2c failed (exit {p.returncode})")

    @staticmethod
    def _notes(d: Download, dest: Path):
        notes = dest / "notes.md"
        src = {"huggingface": f"https://huggingface.co/{d.repo}", "modelscope": f"https://modelscope.cn/models/{d.repo}",
               "ollama": f"https://ollama.com/library/{d.repo}", "url": d.repo}[d.source]
        line = f"- {time.strftime('%Y-%m-%d')}: {', '.join(Path(f['path']).name for f in d.files)} from {src}\n"
        if not notes.exists():
            notes.write_text(f"# {dest.name}\n\nSource: {src}\n\n## Downloads\n", encoding="utf-8")
        with open(notes, "a", encoding="utf-8") as f:
            f.write(line)


class _Cancelled(Exception):
    pass


def _bytes(v: str, unit: str) -> int:
    mult = {"B": 1, "KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "KB": 1e3, "MB": 1e6, "GB": 1e9}.get(unit, 1)
    return int(float(v) * mult)


def _glob_re(pat: str) -> str:
    """'*Q4_K_M*' style patterns (or a plain substring) as a regex on the file path."""
    if any(c in pat for c in "*?"):
        return "^" + re.escape(pat).replace(r"\*", ".*").replace(r"\?", ".") + "$"
    return re.escape(pat)
