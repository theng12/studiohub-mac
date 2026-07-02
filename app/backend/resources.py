"""Host + per-studio resource monitoring.

Host stats come from psutil. Per-studio memory is resolved port → PID → process
tree RSS. On macOS, system-wide psutil.net_connections needs elevated rights,
so we resolve listening PIDs with `lsof` (same-user processes — the studios all
run as this user). Process handles are cached so cpu_percent() deltas are
meaningful across polls.

Apple Silicon note: unified memory means there is no separate VRAM figure —
process RSS + host memory pressure IS the honest picture (SPEC §9).
"""

import subprocess

import psutil

_proc_cache: dict[int, psutil.Process] = {}


def host_stats() -> dict:
    vm = psutil.virtual_memory()
    stats = {
        "total_gb": round(vm.total / 1e9, 2),
        "used_gb": round(vm.used / 1e9, 2),
        "available_gb": round(vm.available / 1e9, 2),
        "percent": vm.percent,
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_count": psutil.cpu_count(),
    }
    try:
        la1, la5, la15 = psutil.getloadavg()
        stats["load_avg"] = [round(la1, 2), round(la5, 2), round(la15, 2)]
    except OSError:
        stats["load_avg"] = None
    return stats


def _listening_pids(port: int) -> list[int]:
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=3,
        )
        return [int(p) for p in out.stdout.split() if p.strip().isdigit()]
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return []


def _proc(pid: int) -> psutil.Process | None:
    cached = _proc_cache.get(pid)
    if cached is not None:
        try:
            if cached.is_running():
                return cached
        except psutil.Error:
            pass
        _proc_cache.pop(pid, None)
    try:
        p = psutil.Process(pid)
        p.cpu_percent(interval=None)  # prime the delta for the next call
        _proc_cache[pid] = p
        return p
    except psutil.Error:
        return None


def studio_process_stats(port: int) -> dict | None:
    """RSS/CPU for the process listening on `port`, including children
    (model workers are often child processes)."""
    pids = _listening_pids(port)
    if not pids:
        return None
    rss = 0
    cpu = 0.0
    counted = 0
    root_pid = pids[0]
    for pid in pids:
        p = _proc(pid)
        if p is None:
            continue
        try:
            group = [p] + p.children(recursive=True)
        except psutil.Error:
            group = [p]
        for member in group:
            try:
                rss += member.memory_info().rss
                cpu += member.cpu_percent(interval=None)
                counted += 1
            except psutil.Error:
                continue
    if counted == 0:
        return None
    return {
        "pid": root_pid,
        "rss_gb": round(rss / 1e9, 2),
        "cpu_percent": round(cpu, 1),
        "processes": counted,
    }
