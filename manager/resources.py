"""What the machine has free right now: GPU (nvidia-smi, or Apple Silicon's unified memory), RAM, CPU, and who owns a port."""
from __future__ import annotations

import platform
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time

import psutil

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def gpus() -> list[dict]:
    """One dict per GPU; empty when there is none we can read."""
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return apple_gpu()
    return nvidia_gpus()


def nvidia_gpus() -> list[dict]:
    """One dict per NVIDIA GPU; empty when nvidia-smi is missing or fails."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    q = "index,name,memory.total,memory.used,utilization.gpu,temperature.gpu,power.draw"
    try:
        out = subprocess.run([exe, f"--query-gpu={q}", "--format=csv,noheader,nounits"], capture_output=True,
                             text=True, timeout=10, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    res = []
    for line in out.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 7:
            continue
        num = lambda s: float(s) if s.replace(".", "", 1).isdigit() else None  # noqa: E731
        total, used = num(f[2]) or 0, num(f[3]) or 0
        res.append({"index": int(f[0]), "name": f[1], "vram_total_mib": total, "vram_used_mib": used,
                    "vram_free_mib": total - used, "util_percent": num(f[4]), "temp_c": num(f[5]),
                    "power_w": num(f[6])})
    return res


_APPLE: dict = {}
_IOREG_NUM = r'"{key}"\s*=\s*(\d+)'


def apple_gpu() -> list[dict]:
    """Apple Silicon: the GPU shares the machine's memory. "VRAM" here is what Metal lets the GPU wire, which macOS caps
    below total RAM (iogpu.wired_limit_mb when set, else ~75% of RAM on 32 GB+ Macs, ~2/3 on smaller ones); use and
    load come from the IOAccelerator statistics, which need no admin rights. No temperature without sudo."""
    if not _APPLE:
        ram = psutil.virtual_memory().total / 2**20
        limit = _sysctl_int("iogpu.wired_limit_mb") or 0
        _APPLE.update(name=_sysctl("machdep.cpu.brand_string") or "Apple GPU",
                      total=float(limit) if limit > 0 else ram * (0.75 if ram >= 32 * 1024 else 2 / 3))
    used, util = 0.0, None
    try:
        out = subprocess.run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"], capture_output=True,
                             text=True, timeout=5).stdout
        m = re.search(_IOREG_NUM.format(key="In use system memory"), out)
        used = int(m.group(1)) / 2**20 if m else 0.0
        m = re.search(_IOREG_NUM.format(key="Device Utilization %"), out)
        util = float(m.group(1)) if m else None
    except (OSError, subprocess.SubprocessError):
        pass
    total = _APPLE["total"]
    return [{"index": 0, "name": f"{_APPLE['name']} (unified memory)", "vram_total_mib": total, "vram_used_mib": used,
             "vram_free_mib": max(0.0, total - used), "util_percent": util, "temp_c": None, "power_w": None,
             "unified": True}]


def _sysctl(name: str) -> str | None:
    try:
        return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _sysctl_int(name: str) -> int | None:
    v = _sysctl(name)
    return int(v) if v and v.isdigit() else None


# in a container /proc/stat is the host's: its own CPU time is in its cgroup (an LXC or Docker hub, say)
_CGROUP_CPU = Path("/sys/fs/cgroup/cpu.stat")
_IN_CONTAINER = sys.platform.startswith("linux") and any(
    Path(p).exists() for p in ("/run/systemd/container", "/.dockerenv", "/run/.containerenv"))
_cg_last: tuple[float, int] | None = None


def cpu_percent() -> float:
    """CPU use since the last call, as a share of this machine's cores (a container's own, not its host's)."""
    global _cg_last
    if _IN_CONTAINER:
        try:
            usage = int(re.search(r"usage_usec (\d+)", _CGROUP_CPU.read_text()).group(1))
            now, prev = time.monotonic(), _cg_last
            _cg_last = (now, usage)
            if not prev or now <= prev[0]:
                return 0.0
            return max(0.0, min(100.0, (usage - prev[1]) / 1e6 / ((now - prev[0]) * (psutil.cpu_count() or 1)) * 100))
        except (OSError, AttributeError, ValueError):
            pass
    return psutil.cpu_percent(interval=None)


def snapshot() -> dict:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return {
        "time": time.time(),
        "gpus": gpus(),
        "ram_total_mib": vm.total / 2**20,
        "ram_available_mib": vm.available / 2**20,
        "ram_percent": vm.percent,
        "swap_used_mib": sw.used / 2**20,
        "cpu_percent": cpu_percent(),
        "cpu_count": psutil.cpu_count(),
    }


def vram_used(snap: dict | None = None) -> float:
    g = (snap or {"gpus": gpus()})["gpus"]
    return sum(x["vram_used_mib"] for x in g)


def vram_free(snap: dict | None = None) -> float:
    g = (snap or {"gpus": gpus()})["gpus"]
    return sum(x["vram_free_mib"] for x in g)


def vram_total(snap: dict | None = None) -> float:
    g = (snap or {"gpus": gpus()})["gpus"]
    return sum(x["vram_total_mib"] for x in g)


def port_owner(port: int) -> psutil.Process | None:
    """The process listening on a TCP port, if any."""
    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, OSError):
        return None
    for c in conns:
        if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
            try:
                return psutil.Process(c.pid)
            except psutil.NoSuchProcess:
                return None
    return None


def tree_usage(pid: int) -> dict:
    """RAM (RSS, MiB) and CPU% of a process and all its children."""
    try:
        p = psutil.Process(pid)
        procs = [p] + p.children(recursive=True)
    except psutil.NoSuchProcess:
        return {"ram_mib": 0.0, "cpu_percent": 0.0, "pids": []}
    rss, cpu, pids = 0, 0.0, []
    for q in procs:
        try:
            rss += q.memory_info().rss
            cpu += q.cpu_percent(interval=None)
            pids.append(q.pid)
        except psutil.Error:
            pass
    return {"ram_mib": rss / 2**20, "cpu_percent": cpu, "pids": pids}
