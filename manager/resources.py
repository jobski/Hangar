"""What the machine has free right now: GPU (via nvidia-smi), RAM, CPU, and who owns a port."""
from __future__ import annotations

import shutil
import subprocess
import time

import psutil

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def gpus() -> list[dict]:
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
        "cpu_percent": psutil.cpu_percent(interval=None),
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
