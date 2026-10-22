"""Switching a machine between models safely: the outgoing model finishes its reply, nothing routes around that,
a superseded start is dropped, and Maya keeps its conversation through a clean stop.

These pin the observable outcomes (what a job writes, which URLs a gateway calls, what a request gets back), not
the internals, so the drain and its gateway behaviour can change shape without these breaking."""
import collections
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from manager.api import create_app
from manager.core import Instance, Job, Manager, Refused
from manager.engines import create
from manager.engines.base import Profile
from manager.roles import Roles
from manager.stats import CounterTracker

os.environ["LLM_MANAGER_TOKEN"] = "testtok"

PEER_ENGINE = "http://10.0.0.9:8082"
PEER_GATEWAY = "http://10.0.0.9:9000"
REAL_SLEEP = time.sleep          # DrainTest patches time.sleep process-wide; keep the real one for the fakes


class FakeEngine:
    """Reports requests_processing from a script of readings, then 0."""
    def __init__(self, busy_readings=()):
        self.readings = list(busy_readings)
        self.scrapes = 0
        self.cleanly = None

    def scrape(self):
        self.scrapes += 1
        return {"strata:requests_processing": self.readings.pop(0) if self.readings else 0}

    def stop_cleanly(self, pid, log):
        return bool(self.cleanly)


class GeneratingEngine(FakeEngine):
    """Busy, and its generated-token counter keeps climbing while the drain waits."""
    COUNTERS = [100, 150, 180]

    def __init__(self):
        super().__init__([1, 1])
        self.counters = list(self.COUNTERS)

    def scrape(self):
        cur = super().scrape()
        cur["llamacpp:tokens_predicted_total"] = self.counters.pop(0) if self.counters else self.COUNTERS[-1]
        return cur


class MutexEngine(FakeEngine):
    """scrape() must not be re-entered: it holds the read open and counts any overlap."""
    def __init__(self):
        super().__init__()
        self.inside, self.overlaps, self.meta = 0, 0, threading.Lock()

    def scrape(self):
        with self.meta:
            self.inside += 1
            if self.inside > 1:
                self.overlaps += 1
        try:
            REAL_SLEEP(0.05)                        # real: DrainTest has patched time.sleep away
            return super().scrape()
        finally:
            with self.meta:
                self.inside -= 1


def manager(engine=None, drain_timeout_s=180):
    mgr = Manager.__new__(Manager)
    mgr.lock = threading.RLock()
    mgr.cfg = {"resources": {"drain_timeout_s": drain_timeout_s}}
    mgr.engines = {"strata": engine or FakeEngine()}
    mgr.instances, mgr.jobs, mgr.job_order, mgr.profiles = {}, {}, collections.deque(), {}
    mgr.pending_usage = {}
    mgr.unsampled = collections.defaultdict(float)
    return mgr


def instance(profile="m", state="ready"):
    inst = Instance(engine="strata", profile=profile, pid=os.getpid(), create_time=0, session=0, log_path=None,
                    ours=True, baseline_vram=0)
    inst.state = state
    inst.alive = lambda: True
    return inst


def job():
    return Job(kind="start", title="t", fn=lambda j: None)


class DrainTest(unittest.TestCase):
    def setUp(self):
        patcher = patch("manager.core.time.sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_busy_reply_is_waited_for_and_new_requests_refused(self):
        eng = FakeEngine([1, 1])
        mgr = manager(eng)
        mgr.instances["strata"] = inst = instance()
        j = job()
        mgr._drain(j, "strata")
        self.assertEqual(inst.state, "stopping")            # the gateways refuse it from the first moment
        self.assertGreaterEqual(eng.scrapes, 3)              # read fresh until the reply ended
        log = "\n".join(j.log)
        self.assertIn("finish its reply", log)
        self.assertIn("finished its reply", log)

    def test_tokens_generated_while_draining_are_counted(self):
        eng = GeneratingEngine()
        mgr = manager(eng)
        mgr.instances["strata"] = inst = instance()
        inst.tracker = CounterTracker(count_first=True)             # as _spawn gives an engine we started
        mgr._drain(job(), "strata")
        self.assertEqual(inst.totals["gen_tokens"], 180)            # the whole climb, first reading included
        self.assertTrue(mgr.pending_usage)                          # and it reaches the usage history

    def test_drain_tokens_reach_the_next_chart_point(self):
        eng = GeneratingEngine()
        mgr = manager(eng)
        mgr.instances["strata"] = inst = instance()
        inst.tracker = CounterTracker(count_first=True)
        mgr._drain(job(), "strata")
        self.assertEqual(mgr.unsampled["gen_tokens"], inst.totals["gen_tokens"])   # the chart sees them too

    def test_a_chart_point_adds_the_drain_tokens_once(self):
        mgr = manager()
        mgr.history = collections.deque(maxlen=5)
        mgr.unsampled.update({"gen_tokens": 12.0, "prompt_tokens": 4.0})
        snap = {"time": 1.0, "gpus": [], "ram_total_mib": 0.0, "ram_available_mib": 0.0, "cpu_percent": 0.0}
        fake = SimpleNamespace(snapshot=lambda: snap, vram_used=lambda s: 0.0)
        with patch("manager.core.resources", fake):
            mgr._sample()                                       # no instances: only the held-over tokens count
        self.assertEqual((mgr.history[-1]["gen_tokens"], mgr.history[-1]["prompt_tokens"]), (12.0, 4.0))
        self.assertFalse(mgr.unsampled)                         # spent, so the next point does not repeat them

    def test_usage_is_counted_while_a_gateway_request_stays_open(self):
        """An open inflight request keeps the drain waiting but must not skip the counters."""
        eng = GeneratingEngine()
        mgr = manager(eng)
        mgr.instances["strata"] = inst = instance()
        inst.tracker = CounterTracker(count_first=True)
        mgr.roles = SimpleNamespace(lock=threading.RLock(), inflight={("", "m"): 1})   # never clears
        for _ in range(3):                                          # the drain polls while that request is open
            self.assertTrue(mgr._answering(inst))
        self.assertEqual(inst.totals["gen_tokens"], 180)            # read every time, inflight or not
        self.assertTrue(mgr.pending_usage)

    def test_collector_and_drain_never_read_counters_at_once(self):
        eng = MutexEngine()
        mgr = manager(eng)
        mgr.instances["strata"] = inst = instance()
        inst.tracker = CounterTracker(count_first=True)             # as _spawn gives an engine we started

        def read():
            for _ in range(3):
                mgr._answering(inst)

        threads = [threading.Thread(target=read) for _ in range(2)]   # the collector thread and the job thread
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(eng.overlaps, 0)                         # one reader of the counters at a time
        self.assertEqual(eng.scrapes, 6)                          # and both did read, none lost

    def test_drain_gives_up_at_its_limit(self):
        mgr = manager(FakeEngine([1] * 100), drain_timeout_s=0)
        mgr.instances["strata"] = instance()
        j = job()
        mgr._drain(j, "strata")
        self.assertIn("stopping it anyway", "\n".join(j.log))

    def test_a_request_open_through_the_gateway_counts(self):
        mgr = manager()
        mgr.instances["strata"] = instance()
        mgr.roles = SimpleNamespace(lock=threading.RLock(), inflight={("", "m"): 1})
        calls = []

        def sleep(s):
            calls.append(s)
            mgr.roles.inflight.clear()                       # the reply ends while the drain waits
        with patch("manager.core.time.sleep", sleep):
            mgr._drain(job(), "strata")
        self.assertEqual(len(calls), 1)

    def test_idle_or_absent_engine_is_not_waited_for(self):
        mgr = manager()
        mgr._drain(job(), "strata")                          # nothing running: nothing to do
        mgr.instances["strata"] = inst = instance(state="starting")
        mgr._drain(job(), "strata")
        self.assertEqual(inst.state, "starting")             # only a ready engine is drained

    def test_stop_uses_a_clean_stop_when_the_engine_has_one(self):
        eng = FakeEngine()
        mgr = manager(eng)
        mgr._forget = lambda *a, **k: True
        for cleanly, killed in ((True, []), (False, [os.getpid()])):
            eng.cleanly, seen = cleanly, []
            mgr.instances["strata"] = instance()
            with patch("manager.core._kill_tree", seen.append):
                mgr._stop(job(), "strata", "test")
            self.assertEqual(seen, killed)


class SupersedeTest(unittest.TestCase):
    def queued(self, mgr, pid, state="queued"):
        j = job()
        j.state, j.start_profile, j.start_target = state, pid, mgr.profiles[pid]
        mgr.jobs[j.id] = j
        mgr.job_order.append(j.id)
        return j

    def test_a_newer_exclusive_start_supersedes(self):
        mgr = manager()
        mgr.profiles = {"glm": Profile("glm", "maya", {}), "qwen": Profile("qwen", "strata", {})}
        first = self.queued(mgr, "glm", "running")
        self.queued(mgr, "qwen")
        self.assertEqual(mgr._superseded(first, mgr.profiles["glm"]), "qwen")

    def test_coexisting_starts_on_other_engines_both_run(self):
        mgr = manager()
        mgr.profiles = {"a": Profile("a", "e1", {}, policy="coexist"), "b": Profile("b", "e2", {}, policy="coexist")}
        first = self.queued(mgr, "a", "running")
        self.queued(mgr, "b")
        self.assertIsNone(mgr._superseded(first, mgr.profiles["a"]))

    def test_an_older_or_finished_start_does_not_supersede(self):
        mgr = manager()
        mgr.profiles = {"glm": Profile("glm", "maya", {}), "qwen": Profile("qwen", "strata", {})}
        self.queued(mgr, "qwen", "done")
        first = self.queued(mgr, "glm", "running")
        self.assertIsNone(mgr._superseded(first, mgr.profiles["glm"]))

    def test_a_start_with_production_job_order_reaches_planning(self):
        mgr = manager()                                             # job_order is a deque, as in production
        mgr.profiles = {"glm": Profile("glm", "maya", {})}
        only = self.queued(mgr, "glm")                              # one queued start, nothing after it
        self.assertIsNone(mgr._superseded(only, mgr.profiles["glm"]))

    def test_a_long_queue_of_starts_is_settled_in_one_pass(self):
        mgr = manager()
        mgr.profiles = {f"p{i}": Profile(f"p{i}", "e", {}, policy="coexist") for i in range(200)}
        jobs = [self.queued(mgr, pid) for pid in mgr.profiles]
        t0 = time.time()
        self.assertEqual(mgr._superseded(jobs[0], mgr.profiles["p0"]), "p199")     # only the last one survives
        self.assertLess(time.time() - t0, 0.5)                                     # was exponential in the queue

    def test_a_refused_start_does_not_cancel_earlier_work(self):
        """a (coexist, e1), then b (exclusive, e2), then c (coexist, e2): c supersedes b, so b must not cancel a."""
        mgr = manager()
        mgr.profiles = {"a": Profile("a", "e1", {}, policy="coexist"), "b": Profile("b", "e2", {}, policy="exclusive"),
                        "c": Profile("c", "e2", {}, policy="coexist")}
        first = self.queued(mgr, "a", "running")
        second = self.queued(mgr, "b")
        self.queued(mgr, "c")
        self.assertEqual(mgr._superseded(second, mgr.profiles["b"]), "c")        # c does cancel b: same engine
        self.assertIsNone(mgr._superseded(first, mgr.profiles["a"]))             # b is itself refused: it cancels nothing

    def start_queued(self, mgr):
        """start() with a submit that only queues: the job's work never runs."""
        mgr.profile = lambda pid: mgr.profiles[pid]

        def submit(kind, title, fn):
            j = Job(kind, title, fn)
            mgr.jobs[j.id] = j
            mgr.job_order.append(j.id)
            return j
        mgr.submit = submit

    def test_a_then_b_then_a_ends_on_a(self):
        mgr = manager()
        mgr.profiles = {"glm": Profile("glm", "maya", {}), "qwen": Profile("qwen", "strata", {})}
        self.start_queued(mgr)
        a1 = mgr.start("glm")
        b = mgr.start("qwen")
        a2 = mgr.start("glm")
        self.assertIsNot(a2, a1)                                          # the old A would be refused: ask again, anew
        self.assertEqual(mgr._superseded(a1, mgr.profiles["glm"]), "glm")  # the same start again does its work
        self.assertEqual(mgr._superseded(b, mgr.profiles["qwen"]), "glm")
        self.assertIsNone(mgr._superseded(a2, mgr.profiles["glm"]))       # the last switch wins: A ends up active

    def test_the_same_start_twice_reuses_the_queued_job(self):
        mgr = manager()
        mgr.profiles = {"glm": Profile("glm", "maya", {})}
        self.start_queued(mgr)
        self.assertIs(mgr.start("glm"), mgr.start("glm"))


class StoppingStateTest(unittest.TestCase):
    def roles(self, state):
        mgr = SimpleNamespace(instances={"strata": SimpleNamespace(profile="m", state=state)},
                              profiles={"m": object()}, lock=threading.RLock(), _settings=lambda: {})
        return Roles(mgr, SimpleNamespace(state={}, all=lambda: {}))

    def test_stopping_is_its_own_state_and_not_routable(self):
        roles = self.roles("stopping")
        t = {"machine": "", "profile": "m"}
        self.assertEqual(roles.target_state(t), "stopping")
        self.assertTrue(roles.stopping("profile:m"))
        self.assertFalse(roles.starting("profile:m"))        # never held in wait_for
        self.assertEqual(roles.candidates("profile:m"), [])

    def test_peer_reports_stopping(self):
        roles = self.roles("ready")
        roles.peers.state = {"pc": {"online": True, "status": {"engines": [
            {"name": "strata", "running": {"profile": "m", "state": "stopping"}}]}}}
        self.assertEqual(roles.target_state({"machine": "pc", "profile": "m"}), "stopping")

    def test_implicit_main_reports_a_stopping_model(self):
        roles = self.roles("stopping")                       # drained for a switch: main has no targets
        self.assertTrue(roles.stopping("main"))              # so the 503 stays switch-aware, with Retry-After
        self.assertEqual(roles.candidates("main"), [])
        self.assertFalse(self.roles("ready").stopping("main"))


class FakeRoles:
    def __init__(self, targets, stopping=False):
        self._targets, self._stopping = targets, stopping

    def candidates(self, model, need=0, who=None):
        return [dict(t) for t in self._targets]

    def stopping(self, model):
        return self._stopping

    def opened(self, t):
        return True

    def closed(self, t):
        pass

    def note_used(self, *a, **k):
        pass

    def public(self):
        return [{"name": "main"}]

    def wake_for(self, model):
        return []

    def starting(self, model):
        return False


class DrainingRoles(FakeRoles):
    """Ready to the first routing lookup, gone from the next one on: the target starts draining mid-route."""
    def __init__(self):
        super().__init__([{"machine": "", "profile": "m"}], stopping=True)
        self.lookups = 0

    def candidates(self, model, need=0, who=None):
        self.lookups += 1
        return super().candidates(model, need, who) if self.lookups == 1 else []


class FakePeers:
    def __init__(self):
        self.state = {"pc": {"online": True, "seen": time.time(), "status": {"engines": [
            {"name": "strata", "port": 8082, "plugin": "strata",
             "running": {"profile": "m", "state": "ready", "engine_key": "k"}}]}}}

    def get(self, name):
        return {"url": PEER_GATEWAY, "token": "peertok"}


class GatewayStoppingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = Path(tempfile.mkdtemp())
        cls.mgr = Manager({"manager": {"host": "127.0.0.1", "port": 9000, "data_dir": d, "logs_dir": d},
                           "resources": {"sample_interval_s": 5}, "engines": {}})

    def post(self, client):
        return client.post("/v1/chat/completions", json={"model": "main", "max_tokens": 5},
                           headers={"Authorization": "Bearer testtok", "X-Hangar-Via": "test", "X-Hangar-Caller": "dev"})

    def test_a_stopping_model_answers_503_at_once(self):
        never = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: self.fail(f"called {req.url}")))
        client = TestClient(create_app(self.mgr, peers=FakePeers(), roles=FakeRoles([], stopping=True), gateway=never))
        t0 = time.time()
        r = self.post(client)
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.headers["x-hangar-state"], "stopping")
        self.assertIn("retry-after", r.headers)
        self.assertLess(time.time() - t0, 5)                   # not held in wait_for

    def test_a_target_that_starts_stopping_mid_route_still_says_stopping(self):
        never = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: self.fail(f"called {req.url}")))
        client = TestClient(create_app(self.mgr, peers=FakePeers(), roles=DrainingRoles(), gateway=never))
        r = self.post(client)
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.headers["x-hangar-state"], "stopping")

    def test_hub_does_not_go_direct_around_a_drain(self):
        seen = []

        def h(req):
            seen.append(str(req.url))
            return httpx.Response(503, headers={"X-Hangar-State": "stopping"}, json={"error": {"message": "stopping"}})
        client = TestClient(create_app(self.mgr, peers=FakePeers(), roles=FakeRoles([{"machine": "pc", "profile": "m"}]),
                                       gateway=httpx.AsyncClient(transport=httpx.MockTransport(h))))
        r = self.post(client)
        self.assertEqual(r.status_code, 503)                            # the peer said stopping, cached view or not
        self.assertEqual(r.headers["x-hangar-state"], "stopping")
        self.assertIn("retry-after", r.headers)
        self.assertEqual(seen, [PEER_GATEWAY + "/v1/chat/completions"])    # never the engine straight


class RealManagerStartTest(unittest.TestCase):
    """The start path on a real Manager (its own job queue and job order), with only planning and the process
    launch stubbed: fakes elsewhere in this file once hid that the job order is a deque."""
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.mgr = mgr = Manager({"manager": {"host": "127.0.0.1", "port": 9000, "data_dir": d, "logs_dir": d},
                                  "resources": {"sample_interval_s": 5}, "engines": {}})
        for name, port in (("maya", 18583), ("strata", 18582)):
            mgr.engines[name] = SimpleNamespace(name=name, port=port, can_update=False, plugin=name)
        mgr.engines["strata"].make_profile = lambda pid, cfg: Profile(pid, "strata", cfg)
        mgr.engines["strata"]._run_config = lambda p: {}           # run_custom reads its arg template from here
        mgr.profiles.update({"glm": Profile("glm", "maya", {}), "qwen": Profile("qwen", "strata", {})})
        mgr.refresh_profiles = lambda **k: None
        mgr.plan = lambda pid, force=False: {"ok": True, "reason": "", "will_stop": [], "policy": "exclusive",
                                             "warnings": [], "needs": {"vram_mib": 0, "ram_mib": 0, "source": "test"}}
        self.spawned = []
        mgr._spawn = lambda job, eng, p: self.spawned.append(p.id)

    def run_queue(self):
        """Run the queued jobs in order, as the worker does: what each ended as."""
        outcomes = []
        while not self.mgr.queue.empty():
            job = self.mgr.queue.get_nowait()
            try:
                job.fn(job)
                outcomes.append((job.start_target.id, "done"))
            except Refused:
                outcomes.append((job.start_target.id, "refused"))
        return outcomes

    def test_a_start_reaches_the_launch(self):
        self.mgr.start("glm")
        self.assertEqual(self.run_queue(), [("glm", "done")])
        self.assertEqual(self.spawned, ["glm"])

    def test_a_then_b_then_a_ends_on_a(self):
        """The same start asked again later does the first one's work: one launch, never A then A again."""
        for pid in ("glm", "qwen", "glm"):
            self.mgr.start(pid)
        self.assertEqual(self.run_queue(), [("glm", "refused"), ("qwen", "refused"), ("glm", "done")])
        self.assertEqual(self.spawned, ["glm"])

    def test_a_custom_run_between_two_starts_is_superseded(self):
        """A one-off run of another model still occupies the engine: the earlier start must not reuse its job."""
        self.mgr.start("glm")
        custom = self.mgr.run_custom("qwen", {})
        self.mgr.start("glm")
        self.assertEqual(self.run_queue(),
                         [("glm", "refused"), (custom.start_target.id, "refused"), ("glm", "done")])
        self.assertTrue(custom.start_target.id.startswith("qwen~custom"))
        self.assertEqual(self.spawned, ["glm"])

    def test_a_start_queued_during_a_drain_wins_without_a_wasted_load(self):
        """A newer start landing while the outgoing model drains: give way, leave it running, never load the loser."""
        self.mgr.plan = lambda pid, force=False: {"ok": True, "reason": "", "will_stop": ["strata"], "policy": "exclusive",
                                                  "warnings": [], "needs": {"vram_mib": 0, "ram_mib": 0, "source": "test"}}
        self.mgr.instances["strata"] = inst = instance(profile="qwen", state="ready")

        def drain(j, engine):
            inst.state = "stopping"                       # what the real drain does first
            self.mgr.start("qwen")                        # a newer conflicting start lands mid-drain

        stopped = []
        self.mgr._drain = drain
        self.mgr._stop = lambda j, engine, reason, expected=None: stopped.append(engine)
        self.mgr._wait_vram_released = lambda j: None     # so a run past the fix fails on the bug, not on VRAM polling

        self.mgr.start("glm")
        j = self.mgr.queue.get_nowait()                   # only the first queued job: the newer start stays queued
        with self.assertRaises(Refused):
            j.fn(j)
        self.assertEqual(self.spawned, [])                # glm never loaded
        self.assertEqual(stopped, [])                     # strata never stopped
        self.assertEqual(inst.state, "ready")             # and it is back in service

    def test_a_start_racing_a_custom_run_still_ends_on_it(self):
        """A start landing while the custom run is being submitted must queue afresh, not reuse the earlier job."""
        real_submit, raced, threads = self.mgr.submit, [], []

        def submit(kind, title, fn):
            job = real_submit(kind, title, fn)
            if not title.startswith("custom run of"):
                return job
            thread = threading.Thread(target=lambda: raced.append(self.mgr.start("glm")))
            thread.start()
            thread.join(0.2)                                        # real time: this class leaves time.sleep alone
            threads.append(thread)
            return job
        self.mgr.submit = submit

        a1 = self.mgr.start("glm")
        self.mgr.run_custom("qwen", {})
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(raced), 1)
        self.assertIsNot(raced[0], a1)
        self.assertEqual(self.run_queue()[-1], ("glm", "done"))
        self.assertEqual(self.spawned, ["glm"])


class MayaCleanStopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "maya-maya-m.json").write_text(json.dumps({"exe": str(root / "maya.exe"), "args": []}))
        self.engine = create("maya", {"plugin": "maya", "dir": str(root), "port": 8083, "_data_dir": str(root)})
        self.profile = self.engine.discover_profiles()["maya-maya-m"]

    def test_conversations_are_kept_unless_turned_off(self):
        self.assertEqual(self.engine.launch(self.profile).env.get("STRATA_GLM_SLOT_KEEP"), "1")
        self.profile.cfg["keep_conversations"] = False
        self.assertNotIn("STRATA_GLM_SLOT_KEEP", self.engine.launch(self.profile).env)

    def test_windows_stop_first_sets_the_conversation_aside(self):
        sent, log = [], []
        with patch("manager.engines.maya.httpx.post", lambda url, **kw: sent.append((url, kw["json"])) or
                   SimpleNamespace(status_code=200)):
            self.engine.active_profile = self.profile
            self.engine._set_aside(log.append)
            self.assertEqual(sent[0][0], "http://127.0.0.1:8083/v1/chat/completions")
            self.assertEqual(sent[0][1]["max_tokens"], 1)                 # a new conversation, not a real request
            self.assertIn("aside", log[0])
            self.profile.cfg["keep_conversations"] = False
            self.engine._set_aside(log.append)
            self.assertEqual(len(sent), 1)                                 # nothing kept: nothing to set aside

    @unittest.skipIf(os.name == "nt", "POSIX signal path; Windows uses Ctrl+C on the console (checked live)")
    def test_clean_stop_lets_maya_end_itself(self):
        out = Path(self.tmp.name) / "stopped"
        code = ("import signal, sys, time, pathlib\n"
                "def stop(*a):\n    pathlib.Path(sys.argv[1]).write_text('clean'); sys.exit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\nprint('up', flush=True)\nwhile True: time.sleep(0.1)\n")
        p = subprocess.Popen([sys.executable, "-c", code, str(out)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.stdout.close)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        p.stdout.readline()
        log = []
        self.assertTrue(self.engine.stop_cleanly(p.pid, log.append))
        self.assertEqual(out.read_text(), "clean")
        self.assertIn("keeping its conversation", log[0])


if __name__ == "__main__":
    unittest.main()
