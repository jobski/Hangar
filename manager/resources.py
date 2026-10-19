"""What the machine has free right now: GPU (nvidia-smi, or Apple Silicon's unified memory), RAM, CPU, and who owns a port."""
from __future__ import annotations

import platform
import json
import math
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
import threading

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


def gpu_process_memory() -> dict[int, float] | None:
    """NVIDIA compute-process MiB across all GPUs; None when the driver cannot attribute memory."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        result = subprocess.run([exe, "--query-compute-apps=pid,used_gpu_memory", "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
        if result.returncode:
            return None
        used = {}
        for line in result.stdout.splitlines():
            pid, memory = (v.strip() for v in line.split(","))
            pid, memory = int(pid), float(memory)
            if memory < 0:
                return None
            used[pid] = used.get(pid, 0.0) + memory
        return used
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


_APPLE: dict = {}
_IOREG_NUM = r'"{key}"\s*=\s*(\d+)'
_apple_metrics_cache: tuple[float, dict] = (0.0, {})
_apple_metrics_lock = threading.Lock()


def apple_gpu_metrics() -> dict:
    """Optional macmon readings; a missing helper or sensor never requires elevated privileges."""
    global _apple_metrics_cache
    with _apple_metrics_lock:
        if time.monotonic() - _apple_metrics_cache[0] < 5:
            return dict(_apple_metrics_cache[1])
        result = {}
        exe = shutil.which("macmon") or next((str(p) for p in (
            Path("/opt/homebrew/bin/macmon"), Path("/usr/local/bin/macmon")) if p.is_file()), None)
        if exe:
            try:
                sample = subprocess.run([exe, "pipe", "-s", "1", "-i", "500"], capture_output=True,
                                        text=True, timeout=3)
                data = json.loads(sample.stdout) if sample.returncode == 0 else {}
                if not isinstance(data, dict):
                    data = {}
                temp = data.get("temp") or {}
                values = {"temp_c": temp.get("gpu_temp_avg") if isinstance(temp, dict) else None,
                          "power_w": data.get("gpu_power"), "active_ratio": data.get("gpu_active_ratio")}
                for key, value in values.items():
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                        continue
                    if (key == "temp_c" and 0 < value < 150 or key == "power_w" and value >= 0
                            or key == "active_ratio" and 0 <= value <= 1):
                        result[key] = float(value)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        _apple_metrics_cache = (time.monotonic(), result)
        return dict(result)


def apple_gpu() -> list[dict]:
    """Apple Silicon: the GPU shares the machine's memory. "VRAM" here is what Metal lets the GPU wire, which macOS caps
    below total RAM (iogpu.wired_limit_mb when set, else ~75% of RAM on 32 GB+ Macs, ~2/3 on smaller ones); use and
    load come from IOAccelerator; optional macmon supplies temperature and GPU power without sudo."""
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
    extra = apple_gpu_metrics()
    if util is None and "active_ratio" in extra:
        util = extra["active_ratio"] * 100
    total = _APPLE["total"]
    return [{"index": 0, "name": f"{_APPLE['name']} (unified memory)", "vram_total_mib": total, "vram_used_mib": used,
             "vram_free_mib": max(0.0, total - used), "util_percent": util,
             "temp_c": extra.get("temp_c"), "power_w": extra.get("power_w"),
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


_cpu_sample = 0.0
_temperature_cache: tuple[float, dict] = (0.0, {})
_temperature_lock = threading.Lock()


def _temperature_kind(source: str, label: str) -> str | None:
    """Only attribute identified sensors; motherboard/ACPI zones are not CPU readings."""
    name = f"{source} {label}".lower()
    if "gpu" in name:
        return None
    if re.search(r"\b(dimm\w*|sodimm\w*|dram|ram|memory|jc42|spd5118)\b", name):
        return "ram"
    if re.search(r"\b(cpu\w*|amdcpu|intelcpu|coretemp|k10temp|zenpower|tctl|tdie)\b", name):
        return "cpu"
    return None


def _windows_temperatures() -> list[tuple[str, str, float]]:
    """Read existing Libre/Open Hardware Monitor WMI providers; never install or start one."""
    try:
        import pythoncom
        import pywintypes
        from win32com.client import GetObject
    except ImportError:
        return []
    rows = []
    pythoncom.CoInitialize()
    try:
        for namespace in ("LibreHardwareMonitor", "OpenHardwareMonitor"):
            try:
                service = GetObject(rf"winmgmts:\\.\root\{namespace}")
                for sensor in service.ExecQuery("SELECT Parent, Name, Value FROM Sensor WHERE SensorType = 'Temperature'"):
                    rows.append((str(sensor.Parent), str(sensor.Name), sensor.Value))
            except pywintypes.com_error:
                continue
            if rows:
                break
    finally:
        pythoncom.CoUninitialize()
    return rows


def temperatures() -> dict:
    """Hottest identified CPU/RAM sensor in Celsius; unavailable readings stay None."""
    global _temperature_cache
    with _temperature_lock:
        now = time.monotonic()
        if _temperature_cache[1] and now - _temperature_cache[0] < 5:
            return dict(_temperature_cache[1])
        rows = []
        if sys.platform == "win32":
            rows = _windows_temperatures()
        else:
            try:
                read = getattr(psutil, "sensors_temperatures", None)
                for source, sensors in (read() if read else {}).items():
                    rows.extend((source, sensor.label, sensor.current) for sensor in sensors)
            except (OSError, RuntimeError, NotImplementedError):
                pass
        result = {"cpu_temp_c": None, "ram_temp_c": None}
        for source, label, value in rows:
            kind = _temperature_kind(source, label)
            try:
                value = float(value)
            except (ValueError, TypeError):
                continue
            if kind and math.isfinite(value) and 0 < value < 150:
                key = kind + "_temp_c"
                result[key] = max(result[key] or value, value)
        _temperature_cache = (time.monotonic(), result)
        return dict(result)


def snapshot(sample_cpu: bool = True) -> dict:
    global _cpu_sample
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    if sample_cpu:
        _cpu_sample = cpu_percent()
    return {
        "time": time.time(),
        "gpus": gpus(),
        "ram_total_mib": vm.total / 2**20,
        "ram_available_mib": vm.available / 2**20,
        "ram_percent": vm.percent,
        "swap_used_mib": sw.used / 2**20,
        "cpu_percent": _cpu_sample,
        "cpu_count": psutil.cpu_count(),
        **temperatures(),
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


class UnknownPortOwner:
    pid = None

    def name(self):
        return "unknown process"

    def cmdline(self):
        raise psutil.AccessDenied()


_port_cache = (0.0, {})
_port_lock = threading.Lock()


def port_owner(port: int, fresh: bool = False) -> psutil.Process | UnknownPortOwner | None:
    """Cache one socket scan for dashboard readers; lifecycle checks always request a fresh scan."""
    global _port_cache
    with _port_lock:
        if fresh or time.monotonic() - _port_cache[0] >= 2:
            try:
                conns = psutil.net_connections(kind="tcp")
            except (psutil.AccessDenied, OSError):
                owners = _lsof_ports()
            else:
                owners = {}
                for conn in conns:
                    if conn.status == psutil.CONN_LISTEN and conn.laddr:
                        owners[conn.laddr.port] = conn.pid
            _port_cache = (time.monotonic(), owners)
        owners = _port_cache[1]
        if port not in owners:
            return None
        if owners[port] is None:
            return UnknownPortOwner()
        try:
            return psutil.Process(owners[port])
        except psutil.NoSuchProcess:
            return None


def _lsof_ports() -> dict:
    exe = shutil.which("lsof")
    if not exe:
        return {}
    try:
        result = subprocess.run([exe, "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"], capture_output=True,
                                text=True, timeout=5)
        owners, pid = {}, None
        for line in result.stdout.splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("n"):
                match = re.search(r":(\d+)$", line)
                if match:
                    owners[int(match.group(1))] = pid
        return owners
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


_procs: dict[int, psutil.Process] = {}       # kept between samples: CPU% is measured since the previous call


def _proc(proc: psutil.Process) -> psutil.Process:
    p = _procs.get(proc.pid)
    if p is None or not p.is_running():      # is_running also catches a reused pid
        p = _procs[proc.pid] = proc
    return p


def tree_usage(pid: int) -> dict:
    """RAM (RSS, MiB) and CPU% of a process and all its children."""
    try:
        p = _proc(psutil.Process(pid))
        procs = [p] + [_proc(c) for c in p.children(recursive=True)]   # a child that has just exited is skipped below
    except psutil.NoSuchProcess:
        return {"ram_mib": 0.0, "cpu_percent": 0.0, "pids": []}
    for gone in [k for k, v in _procs.items() if not v.is_running()]:
        _procs.pop(gone, None)
    rss, cpu, pids = 0, 0.0, []
    for q in procs:
        try:
            rss += q.memory_info().rss
            cpu += q.cpu_percent(interval=None)
            pids.append(q.pid)
        except psutil.Error:
            pass
    return {"ram_mib": rss / 2**20, "cpu_percent": cpu, "pids": pids}
