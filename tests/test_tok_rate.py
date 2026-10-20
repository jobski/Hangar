import threading
import time
import unittest
from collections import defaultdict, deque

from manager import core, peers


class _Inst:
    def __init__(self):
        self.window = deque(maxlen=720)
        self.totals = defaultdict(float)
        self.gauges = {}
        self.started = time.time() - 3600
        self.engine = "strata"


class _Manager(core.Manager):
    """Just enough of Manager for live(): the window math touches nothing else."""
    def __init__(self, inst):
        self.lock = threading.Lock()
        self.instances = {"strata": inst}
        self.engines = {}
        self.store = None

    def log_path(self, engine):
        raise OSError("no log")

    def tail_log(self, engine, lines=100):
        return []


def _streaming(per_sec, seconds):
    """An engine that has produced `per_sec` tokens/s for the last `seconds` (1-second samples)."""
    inst = _Inst()
    now = time.time()
    for i in range(1, seconds + 1):
        inst.window.append((now - i, {"gen_tokens": per_sec, "gen_seconds": 1}))
        inst.totals["gen_tokens"] += per_sec
        inst.totals["gen_seconds"] += 1
    return inst


class InstantRateTest(unittest.TestCase):
    def rate(self, inst):
        return _Manager(inst).live(inst)

    def test_streaming_rate_tracks_throughput(self):
        # a steady 40 tok/s reads near 40 (a hair under from the fixed wall-clock span)
        tps = self.rate(_streaming(40, 20))["instant"]["gen_tps"]
        self.assertTrue(34 <= tps <= 42, tps)

    def test_rate_decays_after_generation_stops(self):
        # a short burst that just ended: the instant rate is already well below the
        # busy-seconds average, and keeps falling as the samples leave the window
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
        # 30s after the only activity, the headline is 0 while the busy-seconds window
        # still holds the last burst's speed (the bug this fixes)
        inst = _Inst()
        now = time.time()
        for dt in (30, 31):
            inst.window.append((now - dt, {"gen_tokens": 80, "gen_seconds": 2}))
        st = self.rate(inst)
        self.assertEqual(st["instant"]["gen_tps"], 0.0)
        self.assertEqual(st["window"]["5m"]["gen_tps"], 40.0)


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
        # stale 1m/5m windows say 40; an idle engine must report 0, not the last burst
        self.assertIn(self.report({"gen_tps": 0.0}, None), (0, None))

    def test_live_rate_wins_over_the_decaying_window(self):
        self.assertEqual(self.report({"gen_tps": 10.0}, {"gen_tps": 38.0}), 38.0)

    def test_decaying_window_is_used_while_generating(self):
        self.assertEqual(self.report({"gen_tps": 12.5}, None), 12.5)


if __name__ == "__main__":
    unittest.main()
