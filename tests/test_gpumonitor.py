import unittest
from unittest.mock import patch

from manager import gpumonitor, peers


class GpumonitorMergeTest(unittest.TestCase):
    def merge(self, cards, gpus):
        with patch.object(gpumonitor, "telemetry", return_value=cards):
            return gpumonitor.merge([dict(g) for g in gpus])

    def test_honest_load_replaces_misleading_kernel_time(self):
        # a card doing pure CUDA work: low kernel time, but drawing most of its power limit
        gpus = [{"index": 0, "name": "RTX 3080", "util_percent": 46.0, "power_w": 205.0}]
        out = self.merge({0: {"load_pct": 82.0, "kernel_pct": 46.0, "limit_w": 250, "throttle": []}}, gpus)
        self.assertEqual(out[0]["load_percent"], 82.0)
        self.assertEqual(out[0]["kernel_percent"], 46.0)
        self.assertEqual(out[0]["power_limit_w"], 250)
        self.assertEqual(out[0]["util_percent"], 46.0)   # raw kernel time left intact for the fallback/tooltip

    def test_only_concerning_throttle_reasons_are_flagged(self):
        gpus = [{"index": 0, "util_percent": 50.0}, {"index": 1, "util_percent": 50.0}]
        cards = {0: {"load_pct": 90.0, "throttle": ["idle", "power cap", "thermal"]},
                 1: {"load_pct": 20.0, "throttle": ["idle", "power cap"]}}
        out = self.merge(cards, gpus)
        self.assertEqual(out[0]["throttling"], ["thermal"])   # reduced clocks / power cap under load are normal
        self.assertEqual(out[1]["throttling"], [])

    def test_cards_the_sidecar_does_not_report_are_left_alone(self):
        out = self.merge({0: {"load_pct": 50.0, "throttle": []}}, [{"index": 7, "util_percent": 12.0}])
        self.assertNotIn("load_percent", out[0])
        self.assertNotIn("throttling", out[0])

    def test_no_sidecar_is_a_no_op(self):
        out = self.merge({}, [{"index": 0, "util_percent": 46.0, "power_w": 205.0}])
        self.assertNotIn("load_percent", out[0])
        self.assertNotIn("power_limit_w", out[0])


class SummaryPropagationTest(unittest.TestCase):
    def status(self, gpus):
        return {"engines": [{"name": "e", "running": None}],
                "resources": {"gpus": gpus, "ram_total_mib": 32000, "ram_available_mib": 8000,
                              "cpu_percent": 10, "cpu_count": 8}}

    def names(self, sm):
        return {g["name"]: g for g in sm["gpu_list"]}

    def test_fleet_load_prefers_honest_and_keeps_kernel_for_the_tooltip(self):
        sm = peers.summary(self.status([
            {"name": "RTX 3080", "vram_used_mib": 9000, "vram_total_mib": 10000, "util_percent": 46.0,
             "power_w": 205.0, "load_percent": 82.0, "kernel_percent": 46.0, "power_limit_w": 250, "throttling": ["thermal"]},
            {"name": "RTX 3090", "vram_used_mib": 2000, "vram_total_mib": 24000, "util_percent": 5.0, "power_w": 20.0},
        ]))
        g = self.names(sm)
        self.assertEqual(g["RTX 3080"]["util_percent"], 82.0)     # honest load is what the fleet view shows
        self.assertEqual(g["RTX 3080"]["kernel_percent"], 46.0)   # kernel time preserved, not the headline
        self.assertEqual(g["RTX 3080"]["power_limit_w"], 250)
        self.assertEqual(g["RTX 3080"]["throttling"], ["thermal"])
        self.assertEqual(g["RTX 3090"]["util_percent"], 5.0)      # no sidecar card: kernel time passes through
        self.assertIsNone(g["RTX 3090"]["kernel_percent"])        # ...and no phantom tooltip field is invented

    def test_fleet_gpu_average_uses_honest_load(self):
        sm = peers.summary(self.status([
            {"name": "A", "vram_used_mib": 1, "vram_total_mib": 2, "util_percent": 46.0, "load_percent": 82.0},
            {"name": "B", "vram_used_mib": 1, "vram_total_mib": 2, "util_percent": 86.0, "load_percent": 3.0},
        ]))
        # mean of the honest numbers (82, 3), not the kernel times (46, 86)
        self.assertAlmostEqual(sm["gpu_util_percent"], 42.5)


if __name__ == "__main__":
    unittest.main()
