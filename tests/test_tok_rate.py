import threading
import time
import unittest
from collections import defaultdict, deque

from manager import core, peers
from manager.engines import Engine


class _Inst:
    def __init__(self):
        self.window = deque(maxlen=720)
        self.totals = defaultdict(float)
        self.gauges = {}
        self.started = time.time() - 3600
        self.engine = "strata"


class _CounterEngine(Engine):
    """A plain server: counters move as it generates; it prints no per-second rate."""
    plugin = "counter"

    def __init__(self):
        super().__init__("llamacpp", {"port": 8081})


class _LiveEngine(Engine):
    """Strata-like: its log prints the live rate; its counters only jump when a request ends."""
    plugin = "live"

    def __init__(self, rate=None):
        super().__init__("strata", {"port": 8082})
        self._rate = rate

    def progress(self, lines):
        return {"phase": "answering", "gen_tps": self._rate} if self._rate is not None else None


class _FreshPath:
    def stat(self):
        return type("S", (), {"st_mtime": time.time() - 1})()


class _Manager(core.Manager):
    """Just enough of Manager for live(): the rate math touches nothing else."""
    def __init__(self, inst, engine=None, fresh=True):
        self.lock = threading.Lock()
        self.instances = {"strata": inst}
        self.engines = {inst.engine: engine} if engine is not None else {}
        self.store = None
        self._fresh = fresh

    def log_path(self, engine):
        if not self._fresh:
            raise OSError("no log")
        return _FreshPath()

    def tail_log(self, engine, lines=100):
        return ["[strata] answering: 100 of max 4096 tokens, 70.0 tok/s, 2 s"]


def _generating(per_sec, seconds):
    """An engine whose counters reported `per_sec` tokens/s for the last `seconds` (1-second samples)."""
    inst = _Inst()
    now = time.time()
    for i in range(1, seconds + 1):
        inst.window.append((now - i, {"gen_tokens": per_sec, "gen_seconds": 1}))
        inst.totals["gen_tokens"] += per_sec
        inst.totals["gen_seconds"] += 1
    return inst


class CounterEngineTest(unittest.TestCase):
    """Engines that report no live rate (plain llama.cpp / mlx): the wall-clock counter rate drives them,
    tracks throughput, and decays to zero when they go quiet."""
    def rate(self, inst):
        return _Manager(inst, _CounterEngine()).live(inst)

    def test_streaming_rate_tracks_throughput(self):
        tps = self.rate(_generating(40, 20))["instant"]["gen_tps"]
        self.assertTrue(34 <= tps <= 42, tps)

    def test_rate_decays_after_generation_stops(self):
        inst = _Inst()
        now = time.time()
        for dt in (1, 2, 3, 4):
            inst.window.append((now - dt, {"gen_tokens": 40, "gen_seconds": 1}))
            inst.totals["gen_tokens"] += 40
            inst.totals["gen_seconds"] += 1
        st = self.rate(inst)
        self.assertLess(st["instant"]["gen_tps"], st["window"]["5m"]["gen_tps"])
        self.assertGreater(st["instant"]["gen_tps"], 0)

    def test_idle_reads_zero_not_the_last_burst(self):
        inst = _Inst()
        now = time.time()
        for dt in (30, 31):
            inst.window.append((now - dt, {"gen_tokens": 80, "gen_seconds": 2}))
        st = self.rate(inst)
        self.assertEqual(st["instant"]["gen_tps"], 0.0)
        self.assertEqual(st["window"]["5m"]["gen_tps"], 40.0)


class LiveEngineTest(unittest.TestCase):
    """Strata: the log's per-second rate is the honest now; the counter rate would read 0 mid-request
    and spike when a request finishes, so it must never be shown for this engine."""
    def test_log_rate_shown_even_while_the_gauge_lags(self):
        # the bug: requests_processing was 0 (the slow scrape lags the log), so live was skipped and the
        # headline fell to the counter rate - now the fresh log drives it regardless of the gauge
        inst = _Inst()
        inst.gauges = {"requests_processing": 0}
        st = _Manager(inst, _LiveEngine(rate=70.0)).live(inst)
        self.assertEqual(st["live"]["gen_tps"], 70.0)

    def test_no_bursty_counter_rate_for_a_live_engine(self):
        # the counter window still holds a big burst; the live engine exposes no instant rate to fall back to
        inst = _generating(40, 60)
        st = _Manager(inst, _LiveEngine(rate=70.0)).live(inst)
        self.assertIsNone(st["instant"])

    def test_idle_live_engine_reports_nothing(self):
        # generation finished: the log's last line is 'done', so progress says nothing - live and instant
        # both None, so the headline reads idle (0), never a stale burst
        inst = _Inst()
        st = _Manager(inst, _LiveEngine(rate=None)).live(inst)
        self.assertIsNone(st["live"])
        self.assertIsNone(st["instant"])


class FleetGateTest(unittest.TestCase):
    def status(self, instant, live):
        return {"engines": [{"name": "e", "port": 8082, "running": {
            "profile": "p", "state": "ready",
            "stats": {"live": live, "instant": instant,
                      "window": {"1m": {"gen_tps": 40.0}, "5m": {"gen_tps": 40.0}}, "gauges": {}}}}],
            "resources": {"gpus": []}}

    def report(self, instant, live):
        return peers.summary(self.status(instant, live))["running"][0]["gen_tps"]

    def test_idle_engine_contributes_nothing_to_the_fleet(self):
        self.assertIn(self.report(None, None), (0, None))

    def test_live_rate_wins_over_the_counter_window(self):
        self.assertEqual(self.report(None, {"gen_tps": 38.0}), 38.0)

    def test_counter_rate_used_when_there_is_no_live_rate(self):
        self.assertEqual(self.report({"gen_tps": 12.5}, None), 12.5)


if __name__ == "__main__":
    unittest.main()
