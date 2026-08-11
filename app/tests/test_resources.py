from types import SimpleNamespace

from backend import resources


class _DeniedProcess:
    pid = 17

    def name(self):
        raise SystemError("macOS denied KERN_PROCARGS2")

    def cmdline(self):
        return []


class _CaddyProcess:
    pid = 23

    def name(self):
        return "caddy"

    def cmdline(self):
        return ["/Applications/Pinokio.app/caddy"]

    def memory_info(self):
        return SimpleNamespace(rss=125_000_000)

    def cpu_percent(self, interval=None):
        return 1.25

    def num_fds(self):
        return 42


def test_proxy_stats_skips_a_process_macOS_refuses_to_inspect(monkeypatch):
    def process_iter(attrs=None):
        assert attrs is None
        return iter([_DeniedProcess(), _CaddyProcess()])

    monkeypatch.setattr(resources.psutil, "process_iter", process_iter)
    monkeypatch.setattr(resources, "_proc", lambda _pid: None)

    assert resources.proxy_stats() == {
        "status": "healthy",
        "processes": 1,
        "pids": [23],
        "rss_gb": 0.12,
        "cpu_percent": 1.2,
        "file_descriptors": 42,
    }
