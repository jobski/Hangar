"""Maya profiles through the engine, custom-run, client and library integration paths."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import psutil

from manager import clients, models
from manager.core import Instance, Manager
from manager.engines import create
from manager.engines.maya import CTRL_C_ON
from manager.engines.strata import StrataEngine
from manager.stats import CounterTracker


class MayaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pack = self.root / "models" / "Maya-M" / "pack"
        self.pack.mkdir(parents=True)
        self.shard = self.pack.parent / "model-00001-of-00001.gguf"
        self.shard.write_bytes(b"x" * 1024 * 1024)
        (self.pack / "sources.json").write_text(json.dumps({"files": [{"name": self.shard.name}]}))
        (self.pack / "dense.bin").write_bytes(b"x" * 1024 * 1024)
        self.config = self.root / "maya-maya-m.json"
        self.rc = {"exe": str(self.root / "maya.exe"), "model_name": "glm-5.3-flash",
                   "args": ["--glm-pack", str(self.pack), "--max-context", "32768", "--expert-cache", "auto"],
                   "vision": {"model": str(self.root / "vision.gguf"), "mmproj": str(self.root / "mmproj.gguf")},
                   "reasoning_effort": "high", "api_key": "config-key"}
        self.config.write_text(json.dumps(self.rc))
        self.ecfg = {"plugin": "maya", "dir": str(self.root), "port": 8082, "host": "127.0.0.1",
                     "_data_dir": str(self.root)}
        self.engine = create("maya", self.ecfg)
        self.profile = self.engine.discover_profiles()["maya-maya-m"]

    def manager(self):
        mgr = Manager.__new__(Manager)
        mgr.profiles = {self.profile.id: self.profile}
        mgr.engines = {"maya": self.engine}
        mgr.instances = {}
        mgr.lock = threading.RLock()
        mgr._settings = lambda: {}
        mgr.refresh_profiles = lambda **kwargs: None
        return mgr

    def test_discovery_is_separate_from_strata_and_settings(self):
        (self.root / "maya-maya-m.shared-settings.json").write_text(json.dumps(self.rc))
        (self.root / "maya-invalid.json").write_text("broken")
        (self.root / "maya-other.json").write_text("{}")
        (self.root / "strata-test.json").write_text(json.dumps(self.rc))
        self.assertEqual(list(self.engine.discover_profiles()), ["maya-maya-m"])
        strata = StrataEngine("strata", self.ecfg)
        self.assertEqual(list(strata.discover_profiles()), ["strata-test"])
        self.assertFalse(self.engine.can_update)
        self.assertEqual(self.profile.policy, "exclusive")
        self.assertTrue(self.engine.has_vision(self.profile))

    def test_custom_run_preserves_engine_args_and_source_config(self):
        mgr = self.manager()
        mgr.submit = lambda kind, title, fn: fn(None)
        mgr._start = lambda job, profile, force, update: profile
        original = self.config.read_bytes()
        custom = mgr.run_custom(self.profile.id, {"ctx": 8192, "args_add": ["--parallel", "2"]})
        launch = self.engine.launch(custom)
        i = launch.cmd.index(str(self.root / "maya.py"))
        self.assertEqual(launch.cmd[i:i + 3], [str(self.root / "maya.py"), "--plain", "--yes"])
        self.assertEqual(launch.cmd[1:i], ["-c", CTRL_C_ON] if os.name == "nt" else [])
        self.assertIn("8082", launch.cmd)
        self.assertNotIn("--engine", launch.cmd)
        generated = Path(launch.cmd[launch.cmd.index("--config") + 1])
        args = json.loads(generated.read_text())["args"]
        self.assertIn("--glm-pack", args)
        self.assertIn("--expert-cache", args)
        self.assertEqual(args[-2:], ["--max-context", "8192"])
        self.assertIn("--parallel", args)
        self.assertEqual(self.engine.launch_context(launch.cmd), 8192)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.engine.match_process(launch.cmd, {custom.id: custom}), custom.id)

    def test_clients_use_mayas_model_context_vision_and_responses(self):
        mgr = self.manager()
        entries = clients.model_entries(mgr, None, "localhost")
        entry = entries[0]
        self.assertEqual(mgr._context(self.profile.id), 32768)
        self.assertEqual(entry["model"], "glm-5.3-flash")
        self.assertEqual(entry["context"], 32768)
        self.assertEqual(entry["api_key"], "config-key")
        self.assertTrue(entry["vision"])
        self.assertTrue(entry["reasoning"])
        qwen = json.loads(clients.gen_qwen(entries, False)["content"])
        self.assertEqual(qwen["modelProviders"]["openai"][0]["wireApi"], "responses")
        self.engine.api_key = "engine-key"
        launch = self.engine.launch(self.profile)
        self.assertEqual(launch.cmd[launch.cmd.index("--api-key") + 1], "engine-key")
        self.assertEqual(clients.model_entries(mgr, None, "localhost")[0]["api_key"], "engine-key")

    def test_pack_backing_shards_are_counted_and_protected_from_deletion(self):
        need = self.engine.estimate(self.profile, 24000, 64000)
        self.assertTrue(need.greedy_vram)
        self.assertGreaterEqual(need.ram_mib, 2)
        self.assertEqual(need.vram_mib, 24000)
        lib = models.Library({"models": {"dir": str(self.root / "models")}, "engines": {"maya": self.ecfg}})
        references = lib._references(self.profile, strict=True)
        self.assertIn(self.shard.resolve(), references)
        self.assertIn((self.root / "mmproj.gguf").resolve(), references)
        with self.assertRaisesRegex(ValueError, "profiles still use"):
            lib.delete(str(self.pack.parent), {self.profile.id: self.profile})
        self.assertTrue(self.shard.exists())

    def test_live_progress_formats_and_completion(self):
        progress = self.engine.progress(["[maya] reading the prompt: 1,200 of 2,400 tokens, 4 s so far"])
        self.assertEqual(progress["prompt_tps"], 300)
        self.assertEqual(progress["of"], 2400)
        progress = self.engine.progress(["[maya] reading the prompt: 1,200 tokens, 0 s so far"])
        self.assertIsNone(progress["prompt_tps"])
        self.assertNotIn("of", progress)
        line = "[maya] writing a tool call: read_file: 100 of max 4096 tokens, 35.5 tok/s, 3 s"
        self.assertEqual(self.engine.progress([line])["gen_tps"], 35.5)
        self.assertIsNone(self.engine.progress([line, "[maya] done: 100 tokens in 3 s (35.5 tok/s)"]))

    def test_reasoning_preserves_shared_settings_and_resets_to_config(self):
        self.engine.active_profile = self.profile
        shared = {"defaults": {"temperature": 0.7, "reasoning_effort": "low"}}
        response = httpx.Response(200, json=shared, request=httpx.Request("GET", "http://localhost/settings"))
        with patch("httpx.get", return_value=response), patch("httpx.post", return_value=httpx.Response(200)) as post:
            self.assertEqual(self.engine.reasoning_get(self.profile), "low")
            self.engine.reasoning_set(self.profile, "max")
            self.assertEqual(post.call_args.kwargs["json"],
                             {"defaults": {"temperature": 0.7, "reasoning_effort": "max"}})
            self.engine.reasoning_set(self.profile, "default")
            self.assertEqual(post.call_args.kwargs["json"], {"defaults": {"temperature": 0.7}})
        self.assertEqual(self.engine.reasoning_levels(self.profile), ["default", "none", "low", "high", "max"])

    def test_maya_metrics_count_requests_as_well_as_tokens(self):
        response = httpx.Response(200, text="maya:requests_total 3\nllamacpp:tokens_predicted_total 125\n")
        with patch("httpx.get", return_value=response):
            metrics = self.engine.scrape()
        delta = CounterTracker(count_first=True).delta(metrics)
        self.assertEqual(delta["requests"], 3)
        self.assertEqual(delta["gen_tokens"], 125)

    def test_launcher_child_owns_port_but_unrelated_process_does_not(self):
        inst = Instance(engine="maya", profile=self.profile.id, pid=100, create_time=1, session=1,
                        log_path=None, ours=True, baseline_vram=0)
        with patch.object(Instance, "alive", return_value=True), patch("psutil.Process") as process:
            process.return_value.parents.return_value = [SimpleNamespace(pid=100)]
            self.assertTrue(inst.owns_process(101))
            self.assertTrue(inst.owns_process(100))
            process.return_value.parents.return_value = [SimpleNamespace(pid=99)]
            self.assertFalse(inst.owns_process(102))
            process.side_effect = psutil.NoSuchProcess(103)
            self.assertFalse(inst.owns_process(103))
        with patch.object(Instance, "alive", return_value=False):
            self.assertFalse(inst.owns_process(100))


if __name__ == "__main__":
    unittest.main()
