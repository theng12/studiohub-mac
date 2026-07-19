"""Host + per-studio resource monitoring.

Host stats come from psutil. Per-studio memory is resolved port → PID → process
tree RSS. On macOS, system-wide psutil.net_connections needs elevated rights,
so we resolve listening PIDs with `lsof` (same-user processes — the studios all
run as this user). Process handles are cached so cpu_percent() deltas are
meaningful across polls.

Apple Silicon note: unified memory means there is no separate VRAM figure —
process RSS + host memory pressure IS the honest picture (SPEC §9).
"""

from functools import lru_cache
import subprocess

import psutil

_proc_cache: dict[int, psutil.Process] = {}
_proxy_alert_state = {"degraded": False}

CADDY_RSS_WARN_GB = 1.0
CADDY_FD_WARN = 1000


@lru_cache(maxsize=1)
def apple_chip_name() -> str | None:
    """Return the Mac's marketing chip name without polling sysctl every tick."""
    try:
        result = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True, timeout=2,
        )
        value = result.stdout.strip()
        return value or None
    except (subprocess.TimeoutExpired, OSError):
        return None


def host_stats() -> dict:
    vm = psutil.virtual_memory()
    stats = {
        "chip": apple_chip_name(),
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


def proxy_stats() -> dict:
    """Inspect Pinokio's Caddy reverse proxy without requiring root.

    Normal Caddy usage is small. A very large descriptor count or RSS is a
    strong signal of a failed configuration reload loop (for example another
    service owning HTTPS port 443), which should be visible before it starves
    generation workers of memory.
    """
    rows = []
    for candidate in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (candidate.info.get("name") or "").lower()
            cmd = candidate.info.get("cmdline") or []
            executable = (cmd[0].rsplit("/", 1)[-1].lower() if cmd else "")
            if name != "caddy" and executable != "caddy":
                continue
            process = _proc(candidate.pid) or candidate
            rows.append({
                "pid": candidate.pid,
                "rss": process.memory_info().rss,
                "cpu": process.cpu_percent(interval=None),
                "fds": process.num_fds() if hasattr(process, "num_fds") else None,
            })
        except (psutil.Error, OSError):
            continue
    rss = sum(row["rss"] for row in rows)
    fds = sum(row["fds"] or 0 for row in rows)
    degraded = bool(rows) and (rss / 1e9 >= CADDY_RSS_WARN_GB or fds >= CADDY_FD_WARN)
    return {
        "status": "degraded" if degraded else ("healthy" if rows else "not_running"),
        "processes": len(rows),
        "pids": [row["pid"] for row in rows],
        "rss_gb": round(rss / 1e9, 2),
        "cpu_percent": round(sum(row["cpu"] for row in rows), 1),
        "file_descriptors": fds if rows else None,
    }


def check_proxy_health() -> dict:
    """Emit one alert per Caddy degradation/recovery edge."""
    stats = proxy_stats()
    degraded = stats["status"] == "degraded"
    if degraded and not _proxy_alert_state["degraded"]:
        from . import alerts
        alerts.emit(
            "proxy_degraded",
            "Pinokio Caddy is consuming abnormal resources; check for a port 443 conflict",
            stats,
        )
        _proxy_alert_state["degraded"] = True
    elif stats["status"] == "healthy" and _proxy_alert_state["degraded"]:
        from . import alerts
        alerts.emit("proxy_recovered", "Pinokio Caddy resource use returned to normal", stats)
        _proxy_alert_state["degraded"] = False
    return stats
