"""The machine log: every engine's log and Hangar's events as one tagged stream, so a switch from one engine to
another shows without picking a log. Pinned on what a reader sees, step by step."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from manager.api import create_app
from manager.core import Instance, Manager
from manager.engines.base import Profile

os.environ["LLM_MANAGER_TOKEN"] = "testtok"


class MachineLogTest(unittest.TestCase):
    def setUp(self):
        self.d = d = Path(tempfile.mkdtemp())
        self.mgr = mgr = Manager({"manager": {"host": "127.0.0.1", "port": 9000, "data_dir": d, "logs_dir": d},
                                  "resources": {"sample_interval_s": 5}, "engines": {}})
        self.own = d / "maya-engine.log"
        mgr.engines.update({"strata": SimpleNamespace(extra_logs=lambda p: []),
                            "maya": SimpleNamespace(extra_logs=lambda p: [self.own])})
        mgr.profiles.update({"qwen": Profile("qwen", "strata", {}), "glm": Profile("glm", "maya", {})})
        self.log("strata", "strata: old line\nstrata: ready\n")
        self.log("maya", "maya: from yesterday\n")

    def log(self, engine, text, mode="a"):
        with open(self.d / f"{engine}.log", mode, encoding="utf-8") as f:
            f.write(text)

    def run_engine(self, engine, profile):
        self.mgr.instances[engine] = Instance(engine=engine, profile=profile, pid=os.getpid(), create_time=0, session=0,
                                              log_path=str(self.d / f"{engine}.log"), ours=True, baseline_vram=0)

    def stop_engine(self, engine):
        self.mgr.instances.pop(engine, None)

    def test_it_opens_with_events_and_what_runs_then_follows_a_switch(self):
        self.mgr.store.event("start", "strata-iq3_s ready after 44 s")
        self.run_engine("strata", "qwen")
        text, state = self.mgr.machine_log(None, 20)
        self.assertIn("[hangar]", text)
        self.assertIn("strata-iq3_s ready after 44 s", text)
        self.assertIn("[strata] strata: ready", text)
        self.assertNotIn("from yesterday", text)                      # not running: its old lines stay out
        # the switch: strata stops, maya starts and writes - no log has to be picked
        self.stop_engine("strata")
        self.mgr.store.event("stop", "strata-iq3_s: making room for maya-maya-m")
        self.run_engine("maya", "glm")
        self.log("maya", "maya: loading the pack\n")
        self.own.write_text("glm slots: 2 conversation(s) kept\n")
        text, state = self.mgr.machine_log(state)
        self.assertIn("⇄ switching strata-iq3_s → maya-maya-m", text)   # a switch, told apart from a plain stop
        self.assertIn("[maya] maya: loading the pack", text)
        self.assertNotIn("strata: ready", text)                      # only what is new
        self.log("strata", "strata: a straggler\n")
        self.own.write_text("glm slots: 2 conversation(s) kept\nglm slots: saved a 4893-token conversation\n")
        text, state = self.mgr.machine_log(state)
        self.assertIn("[strata] strata: a straggler", text)          # every engine is followed, running or not
        self.assertIn("[maya:engine] glm slots: saved a 4893-token conversation", text)
        self.assertEqual(self.mgr.machine_log(state)[0], "")         # nothing new: nothing

    def test_a_rotated_log_is_read_from_its_start(self):
        self.run_engine("strata", "qwen")
        _, state = self.mgr.machine_log(None, 5)
        (self.d / "strata.log").rename(self.d / "strata.old.log")
        self.log("strata", "===== start qwen\nstrata: fresh\n", mode="w")
        text, _ = self.mgr.machine_log(state)
        self.assertIn("[strata] ===== start qwen", text)
        self.assertIn("[strata] strata: fresh", text)

    def test_half_a_line_waits_for_the_rest_then_shows_anyway(self):
        self.run_engine("strata", "qwen")
        _, state = self.mgr.machine_log(None, 5)
        self.log("strata", "reading the prompt: 12,000 of")
        text, state = self.mgr.machine_log(state)
        self.assertNotIn("reading the prompt", text)
        self.log("strata", " 39,584 tokens\n")
        text, state = self.mgr.machine_log(state)
        self.assertIn("[strata] reading the prompt: 12,000 of 39,584 tokens", text)
        self.log("strata", "waiting forever")
        _, state = self.mgr.machine_log(state)
        later = time.time() + 10                                       # the clock moves on, the line stays unfinished
        with patch("manager.core.time.time", lambda: later):
            text, state = self.mgr.machine_log(state)
        self.assertIn("[strata] waiting forever", text)

    def test_progress_rewritten_with_a_carriage_return_is_split(self):
        self.run_engine("strata", "qwen")
        _, state = self.mgr.machine_log(None, 5)
        self.log("strata", "10 s so far\r20 s so far\rdone\n")
        text, _ = self.mgr.machine_log(state)
        self.assertEqual([l for l in text.splitlines() if l.startswith("[strata]")],
                         ["[strata] 10 s so far", "[strata] 20 s so far", "[strata] done"])

    def test_an_engine_started_later_shows_its_own_log_from_its_first_line(self):
        self.own.write_text("glm slots: from an earlier run\n")
        _, state = self.mgr.machine_log(None, 10)                     # maya not running yet
        self.run_engine("maya", "glm")
        with open(self.own, "a") as f:
            f.write("glm fast: the first start-up line\n")
        text, _ = self.mgr.machine_log(state)
        self.assertIn("[maya:engine] glm fast: the first start-up line", text)
        self.assertNotIn("from an earlier run", text)

    def test_an_unfinished_line_is_not_glued_onto_a_rotated_file(self):
        self.run_engine("strata", "qwen")
        _, state = self.mgr.machine_log(None, 5)
        self.log("strata", "old unfinished")
        _, state = self.mgr.machine_log(state)
        (self.d / "strata.log").rename(self.d / "strata.old.log")
        self.log("strata", "new file line\n", mode="w")
        text, _ = self.mgr.machine_log(state)
        self.assertIn("[strata] old unfinished\n", text)
        self.assertIn("[strata] new file line", text)
        self.assertNotIn("unfinishednew", text)

    def test_a_character_split_across_reads_survives(self):
        self.run_engine("strata", "qwen")
        _, state = self.mgr.machine_log(None, 5)
        with open(self.d / "strata.log", "ab") as f:
            f.write("café".encode()[:-1])                              # the é cut in half
        _, state = self.mgr.machine_log(state)
        with open(self.d / "strata.log", "ab") as f:
            f.write("café".encode()[-1:] + b"\n")
        text, _ = self.mgr.machine_log(state)
        self.assertIn("[strata] café", text)

    def test_a_log_shared_by_two_profiles_counts_as_running_if_either_runs(self):
        self.mgr.profiles = {"idle": Profile("idle", "maya", {}), "glm": Profile("glm", "maya", {})}
        self.own.write_text("glm fast: already running\n")
        self.run_engine("maya", "glm")
        text, _ = self.mgr.machine_log(None, 10)
        self.assertIn("[maya:engine] glm fast: already running", text)

    def test_a_profile_added_later_shows_from_its_first_line(self):
        _, state = self.mgr.machine_log(None, 10)
        late = self.d / "late-engine.log"
        late.write_text("late: first line\n")
        self.mgr.profiles["late"] = Profile("late", "strata", {})
        self.mgr.engines["strata"] = SimpleNamespace(extra_logs=lambda p: [late] if p.id == "late" else [])
        text, _ = self.mgr.machine_log(state)
        self.assertIn("[strata:engine] late: first line", text)

    def test_nothing_written_while_the_opening_is_read_is_lost(self):
        self.run_engine("strata", "qwen")
        real = __import__("manager.core", fromlist=["_tail_file"])._tail_file

        def tail_then_append(path, lines, **kw):
            result = real(path, lines, **kw)
            if path.name == "strata.log":
                self.log("strata", "strata: written meanwhile\n")
            return result
        with patch("manager.core._tail_file", tail_then_append):
            _, state = self.mgr.machine_log(None, 10)
        text, _ = self.mgr.machine_log(state)
        self.assertIn("[strata] strata: written meanwhile", text)

    def test_opening_on_a_half_written_line_keeps_it_whole(self):
        self.run_engine("strata", "qwen")
        with open(self.d / "strata.log", "ab") as f:
            f.write("loading café".encode()[:-1])                      # opened while a line and the é are half written
        text, state = self.mgr.machine_log(None, 10)
        self.assertNotIn("loading", text)
        with open(self.d / "strata.log", "ab") as f:
            f.write("loading café".encode()[-1:] + b" done\n")
        text, _ = self.mgr.machine_log(state)
        self.assertIn("[strata] loading café done", text)

    def test_a_rotation_during_the_opening_is_not_skipped(self):
        self.run_engine("strata", "qwen")
        real = __import__("manager.core", fromlist=["_tail_file"])._tail_file

        def tail_then_rotate(path, lines, **kw):
            result = real(path, lines, **kw)
            if path.name == "strata.log":
                path.rename(path.with_suffix(".old.log"))
                self.log("strata", "x" * 300 + "\nstrata: fresh start\n", mode="w")   # already past the old offset
            return result
        with patch("manager.core._tail_file", tail_then_rotate):
            _, state = self.mgr.machine_log(None, 10)
        text, _ = self.mgr.machine_log(state)
        self.assertIn("[strata] strata: fresh start", text)

    def test_an_idle_logs_unfinished_last_line_is_not_replayed(self):
        self.log("maya", "old unfinished")
        _, state = self.mgr.machine_log(None, 10)                     # maya is not running
        later = time.time() + 10
        with patch("manager.core.time.time", lambda: later):
            text, _ = self.mgr.machine_log(state)
        self.assertNotIn("old unfinished", text)

    def test_a_switch_reads_apart_from_a_plain_stop_or_start(self):
        for kind, msg in (("stop", "strata-iq3_s: making room for maya-maya-m"), ("start", "starting maya-maya-m"),
                          ("ready", "maya-maya-m ready after 74 s"), ("stop", "maya-maya-m: stopped by request"),
                          ("switch", "maya-maya-m idle for 15 min: back to strata-iq3_s"),
                          ("crash", "strata-iq3_s exited unexpectedly"), ("start", "failed\nTraceback: boom")):
            self.mgr.store.event(kind, msg)
        text, _ = self.mgr.machine_log(None, 40)
        self.assertIn("⇄ switching strata-iq3_s → maya-maya-m", text)
        self.assertIn("▶ starting maya-maya-m", text)
        self.assertIn("✓ maya-maya-m ready after 74 s", text)
        self.assertIn("■ stopped maya-maya-m: stopped by request", text)
        self.assertIn("⇄ maya-maya-m idle for 15 min: back to strata-iq3_s", text)
        self.assertIn("✗ strata-iq3_s exited unexpectedly", text)
        self.assertIn("[hangar]   Traceback: boom", text)                # every line of a message keeps its tag

    def test_a_snapshot_shows_an_unfinished_last_line(self):
        self.run_engine("strata", "qwen")
        self.log("strata", "fatal: failed to load")
        self.assertIn("[strata] fatal: failed to load", self.mgr.machine_log(None, 10, snapshot=True)[0])
        self.assertNotIn("fatal", self.mgr.machine_log(None, 10)[0])          # a stream waits for the rest

    def test_the_first_event_ever_is_not_skipped(self):
        _, state = self.mgr.machine_log(None, 10)                     # an empty event store
        self.mgr.store.event("start", "starting strata-iq3_s")
        self.assertIn("▶ starting strata-iq3_s", self.mgr.machine_log(state)[0])

    def test_a_snapshot_shows_a_stopped_engines_last_lines(self):
        self.log("maya", "maya: fatal: out of memory\n")                 # maya stopped (crashed) - not running
        self.assertIn("[maya] maya: fatal: out of memory", self.mgr.machine_log(None, 10, snapshot=True)[0])

    def test_following_one_engine_with_no_history_starts_at_the_end(self):
        self.run_engine("strata", "qwen")
        self.log("strata", "old unfinished")
        data, pos, ident = self.mgr.follow_log("strata", None, lines=0)
        self.assertEqual(self.mgr.follow_log("strata", pos, ident)[0], b"")        # nothing new: nothing
        self.assertNotIn(b"strata: ready", data)
        self.log("strata", "\nstrata: new\n")
        self.assertEqual(self.mgr.follow_log("strata", pos, ident)[0], b"\nstrata: new\n")

    def test_a_snapshot_gives_the_lines_asked_for(self):
        self.run_engine("strata", "qwen")
        self.log("strata", "".join(f"line {i}\n" for i in range(1, 11)), mode="w")
        got = [l for l in self.mgr.machine_log(None, 10, snapshot=True)[0].splitlines() if l.startswith("[strata]")]
        self.assertEqual(got, [f"[strata] line {i}" for i in range(1, 11)])

    def test_a_huge_unfinished_line_stays_pending_whole(self):
        self.run_engine("strata", "qwen")
        self.log("strata", "x" * (300 * 1024) + "caf", mode="w")
        with open(self.d / "strata.log", "ab") as f:
            f.write("é".encode()[:1])                                   # cut in half, far past the read window
        text, state = self.mgr.machine_log(None, 10)
        self.assertNotIn("\ufffd", text)
        with open(self.d / "strata.log", "ab") as f:
            f.write("é".encode()[1:] + b" done\n")
        text = ""
        for _ in range(3):                                             # it may take a few 1 MiB reads
            step, state = self.mgr.machine_log(state)
            text += step
            if "done" in text:
                break
        self.assertIn("[strata] " + "x" * (300 * 1024) + "café done", text)   # the whole line, from its start
        self.assertNotIn("\ufffd", text)

    def test_an_idle_or_history_free_follow_skips_a_huge_unfinished_line(self):
        self.log("maya", "x" * (300 * 1024), mode="w")                 # idle maya, one huge unfinished line
        _, state = self.mgr.machine_log(None, 10)
        later = time.time() + 10
        with patch("manager.core.time.time", lambda: later):
            self.assertEqual(self.mgr.machine_log(state)[0], "")
        self.run_engine("maya", "glm")
        data, pos, ident = self.mgr.follow_log("maya", None, lines=0)
        self.assertEqual(self.mgr.follow_log("maya", pos, ident)[0], b"")

    def test_an_engine_starting_while_the_opening_is_read_is_not_lost(self):
        real = __import__("manager.core", fromlist=["_tail_file"])._tail_file

        def start_maya_meanwhile(path, lines, **kw):
            if path.name == "maya.log" and "maya" not in self.mgr.instances:
                self.log("maya", "maya: starting up\n")
                self.run_engine("maya", "glm")
            return real(path, lines, **kw)
        with patch("manager.core._tail_file", start_maya_meanwhile):
            first, state = self.mgr.machine_log(None, 10)
        later, _ = self.mgr.machine_log(state)
        self.assertIn("[maya] maya: starting up", first + later)

    def test_an_engine_exiting_while_the_opening_is_read_keeps_its_last_words(self):
        self.run_engine("strata", "qwen")
        self.log("strata", "strata: fatal: lost the GPU\n")
        real = __import__("manager.core", fromlist=["_tail_file"])._tail_file

        def exit_meanwhile(path, lines, **kw):
            result = real(path, lines, **kw)
            if path.name == "strata.log":
                self.stop_engine("strata")
            return result
        with patch("manager.core._tail_file", exit_meanwhile):
            text, _ = self.mgr.machine_log(None, 10)
        self.assertIn("[strata] strata: fatal: lost the GPU", text)

    def test_a_new_half_line_gets_its_own_timer(self):
        self.run_engine("strata", "qwen")
        _, state = self.mgr.machine_log(None, 5)
        clock = [1000.0]
        with patch("manager.core.time.time", lambda: clock[0]):
            self.log("strata", "A")
            _, state = self.mgr.machine_log(state)
            clock[0] = 1003.0
            self.log("strata", " done\nB")
            text, state = self.mgr.machine_log(state)
            self.assertIn("[strata] A done", text)
            clock[0] = 1003.5                                          # B is only half a second old: still pending
            text, state = self.mgr.machine_log(state)
            self.assertNotIn("[strata] B", text)
            self.log("strata", " done\n")
            clock[0] = 1004.0
            text, state = self.mgr.machine_log(state)
            self.assertIn("[strata] B done", text)

    def test_snapshot_endpoint(self):
        self.run_engine("strata", "qwen")
        client = TestClient(create_app(self.mgr, peers=SimpleNamespace(state={}, all=lambda: {}),
                                       roles=SimpleNamespace()))
        r = client.get("/api/machine-log?lines=10", headers={"Authorization": "Bearer testtok"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("[strata] strata: ready", r.text)


if __name__ == "__main__":
    unittest.main()
