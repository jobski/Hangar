"""Switching a machine's model because a request named a switching role: the hub relays to the owner, the owner brings
the model up while the request waits (a stream gets progress the clients accept), and that progress never comes back
into the conversation. Pinned on outcomes: what a request gets back, which URLs are called with which headers."""
import json
import os
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
from manager.core import Instance, Job, Manager
from manager.engines.base import Profile
from manager.roles import Roles
from manager.stats import RequestMeter
from manager.switching import PROGRESS, keepalive, stream_error, strip_progress

os.environ["LLM_MANAGER_TOKEN"] = "testtok"
PEER_GATEWAY = "http://10.0.0.9:9000"


class ProgressTest(unittest.TestCase):
    def test_progress_echoed_back_is_stripped(self):
        data = {"messages": [
            {"role": "user", "content": "plan it"},
            {"role": "assistant", "reasoning_content": PROGRESS + "loading GLM - 10 s of ~75 s\nreal thought",
             "content": "<thinking>" + PROGRESS + "loading GLM - 20 s\n</thinking>the plan"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": PROGRESS + "loading\n"},
                                              {"type": "text", "text": "ok"}]}]}
        out = strip_progress(data)
        self.assertEqual(out["messages"][0], data["messages"][0])
        self.assertEqual(out["messages"][1]["reasoning_content"], "real thought")
        self.assertEqual(out["messages"][1]["content"], "the plan")
        self.assertEqual(out["messages"][2]["content"], [{"type": "text", "text": "ok"}])

    def test_a_trimmed_echo_keeps_the_answer(self):
        data = {"messages": [{"role": "assistant", "content": "<thinking>" + PROGRESS + "loading</thinking>The answer is 42."}]}
        self.assertEqual(strip_progress(data)["messages"][0]["content"], "The answer is 42.")

    def test_an_ordinary_hangar_log_line_is_kept(self):
        quoted = "Observed log:\n[hangar] failed to bind port 9000\n"
        data = {"messages": [{"role": "assistant", "content": quoted, "reasoning_content": "[hangar] was here"},
                             {"role": "assistant", "content": PROGRESS + "loading\n" + quoted}]}
        out = strip_progress(data)
        self.assertEqual(out["messages"][0]["content"], quoted)
        self.assertEqual(out["messages"][0]["reasoning_content"], "[hangar] was here")
        self.assertEqual(out["messages"][1]["content"], quoted)

    def test_untouched_without_progress(self):
        data = {"messages": [{"role": "assistant", "content": "<thinking></thinking>kept as sent"}]}
        self.assertIs(strip_progress(data), data)

    def test_progress_is_not_the_models_first_token(self):
        meter = RequestMeter(0.0, True)
        meter.feed(keepalive("chat/completions", "glm", "loading"), 0.1)
        meter.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n', 75.0)
        self.assertEqual((meter.first, meter.token_at), (75.0, 75.0))

    def test_keepalive_forms(self):
        chunk = json.loads(keepalive("chat/completions", "glm", "loading").decode()[len("data: "):])
        self.assertEqual(chunk["choices"][0]["delta"]["reasoning_content"], PROGRESS + "loading\n")
        self.assertIn(b"event: ping", keepalive("messages", "glm", "loading"))
        self.assertTrue(stream_error("chat/completions", "no").endswith(b"data: [DONE]\n\n"))
        self.assertIn(b"event: error", stream_error("messages", "no"))


def roles_with(role, state="stopped", follows=False, peer_state=None):
    settings = {"roles": {"glm": role}, "main_follows_running": follows}
    instances = {"maya": SimpleNamespace(profile="glm-p", state=state)} if state != "stopped" else {}
    mgr = SimpleNamespace(instances=instances, profiles={"glm-p": object()}, lock=threading.RLock(),
                          _settings=lambda: settings, settings_lock=threading.RLock(),
                          write_settings=settings.update, store=SimpleNamespace(event=lambda *a, **k: None))
    peers = SimpleNamespace(state=peer_state or {}, all=lambda: {"pc": {}})
    return Roles(mgr, peers)


class SwitchTargetTest(unittest.TestCase):
    def test_a_switching_role_whose_model_is_down(self):
        t = {"machine": "", "profile": "glm-p"}
        self.assertEqual(roles_with({"targets": [t], "switch": True}).switch_target("glm"), t)
        self.assertIsNone(roles_with({"targets": [t], "switch": True}, state="ready").switch_target("glm"))
        self.assertIsNone(roles_with({"targets": [t]}).switch_target("glm"))              # switching is opt-in

    def test_a_removed_profile_is_not_switched_to(self):
        self.assertIsNone(roles_with({"targets": [{"machine": "", "profile": "gone"}], "switch": True}).switch_target("glm"))

    def test_never_for_a_main_that_follows_the_running_model(self):
        r = roles_with({"targets": [{"machine": "", "profile": "glm-p"}], "switch": True}, follows=True)
        r.mgr._settings()["roles"]["main"] = r.mgr._settings()["roles"]["glm"]
        self.assertIsNone(r.switch_target("main"))

    def test_an_offline_machine_is_woken_and_fallen_back_from_not_switched(self):
        r = roles_with({"targets": [{"machine": "pc", "profile": "glm-p"}], "switch": True},
                       peer_state={"pc": {"online": False, "status": {}}})
        self.assertIsNone(r.switch_target("glm"))

    def test_an_online_peer_with_a_failing_engine_probe_still_switches(self):
        """Its manager is online and the model there is loading: switch to it, do not bypass the owner's wait."""
        with patch("manager.roles.engine_health", return_value={"online": False}):
            r = roles_with({"targets": [{"machine": "pc", "profile": "glm-p"}], "switch": True},
                           peer_state={"pc": {"online": True, "status": {"engines": [
                               {"name": "maya", "running": {"profile": "glm-p", "state": "starting"}}]}}})
            self.assertEqual(r.switch_target("glm"), {"machine": "pc", "profile": "glm-p"})

    def test_save_keeps_and_shows_the_switch_option(self):
        r = roles_with({"targets": [{"machine": "", "profile": "glm-p"}]})
        r.save("glm", [{"profile": "glm-p"}], switch=True)
        self.assertTrue(r.mgr._settings()["roles"]["glm"]["switch"])
        r.save("glm", [{"profile": "glm-p"}])                                              # not given: kept
        self.assertTrue(r.mgr._settings()["roles"]["glm"]["switch"])


class RequestSwitchTest(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.mgr = mgr = Manager({"manager": {"host": "127.0.0.1", "port": 9000, "data_dir": d, "logs_dir": d},
                                  "resources": {"sample_interval_s": 5}, "engines": {}})
        mgr.profiles.update({"glm": Profile("glm", "maya", {}), "qwen": Profile("qwen", "strata", {})})
        self.started = []
        mgr.start = lambda pid, force=False, update="auto": self.started.append(pid) or self.job(pid)

    def job(self, pid, state="queued", error=None):
        j = Job(kind="start", title=pid, fn=lambda j: None)
        j.state, j.error = state, error
        return j

    def running(self, profile, engine, ready_ago):
        inst = Instance(engine=engine, profile=profile, pid=os.getpid(), create_time=0, session=0, log_path=None,
                        ours=True, baseline_vram=0)
        inst.state, inst.ready = "ready", time.time() - ready_ago
        self.mgr.instances[engine] = inst
        return inst

    def test_switching_settings_are_validated(self):
        for bad in ({"max_wait_s": None}, {"max_wait_s": 0}, {"min_turn_minutes": "soon"}, {"min_turn_minutes": -1}, {"colour": 1},
                    {"home": "nope"}):
            with self.assertRaises(ValueError):
                self.mgr.set_switching(bad)
        self.assertEqual(self.mgr.set_switching({"home": "qwen", "min_turn_minutes": 3})["min_turn_minutes"], 3)

    def test_ready_needs_nothing(self):
        self.running("glm", "maya", 999)
        self.assertEqual(self.mgr.request_switch("glm")["state"], "ready")
        self.assertEqual(self.started, [])

    def test_a_model_inside_its_minimum_turn_is_waited_for(self):
        self.running("qwen", "strata", 30)                                # loaded 30 s ago; the turn is 2 min
        res = self.mgr.request_switch("glm")
        self.assertEqual(res["state"], "waiting")
        self.assertAlmostEqual(res["wait_s"], 90, delta=2)
        self.assertEqual(self.started, [])
        self.mgr.instances["strata"].ready -= 100                         # its turn is over
        self.assertEqual(self.mgr.request_switch("glm")["state"], "switching")
        self.assertEqual(self.started, ["glm"])

    def test_a_model_still_loading_keeps_the_gpu(self):
        """qwen has not had its turn yet: queue glm rather than superseding the start and losing glm's own model."""
        inst = self.running("qwen", "strata", 30)
        inst.state, inst.ready = "starting", None
        res = self.mgr.request_switch("glm")
        self.assertEqual(res["state"], "waiting")
        self.assertIn("still loading", res["reason"])
        self.assertAlmostEqual(res["wait_s"], 120, delta=1)               # its whole turn is still ahead
        self.assertEqual(self.started, [])

    def test_a_start_in_progress_holds_the_gpu(self):
        """qwen's start stopped what was here and is still loading: no instance yet, but its turn is still ahead."""
        job = self.job("qwen", "running")
        job.start_target = self.mgr.profiles["qwen"]
        self.mgr.current_job, self.mgr.jobs[job.id] = job, job
        res = self.mgr.request_switch("glm")
        self.assertEqual(res["state"], "waiting")
        self.assertIn("being loaded", res["reason"])
        self.assertAlmostEqual(res["wait_s"], 120, delta=1)           # its whole turn is still ahead
        self.assertEqual(self.started, [])
        job.start_target = self.mgr.profiles["glm"]                   # the start is the model we want: no waiting
        self.assertEqual(self.mgr.request_switch("glm")["state"], "switching")
        self.assertEqual(self.started, ["glm"])

    def test_a_queued_start_of_another_model_holds_the_gpu(self):
        queued = self.job("qwen", "queued")
        queued.start_target = self.mgr.profiles["qwen"]
        self.mgr.jobs[queued.id] = queued
        self.assertEqual(self.mgr.request_switch("glm")["state"], "waiting")
        self.assertEqual(self.started, [])

    def test_two_requests_at_once_never_both_pass(self):
        """Checking and queueing are one step: a request that lands between another's check and its queueing waits."""
        real = self.mgr.start

        def slow_start(pid, force=False, update="auto"):
            job = real(pid, force, update)
            job.start_target = self.mgr.profiles[pid]
            self.mgr.jobs[job.id] = job
            return job
        self.mgr.start = slow_start
        results = {}
        checked = threading.Event()
        orig = self.mgr._request_switch

        def pausing(p, now):
            if p.id == "glm":
                checked.set()
                time.sleep(0.2)                                       # the other request arrives meanwhile
            return orig(p, now)
        self.mgr._request_switch = pausing
        a = threading.Thread(target=lambda: results.update(glm=self.mgr.request_switch("glm")))
        a.start()
        checked.wait(1)
        results["qwen"] = self.mgr.request_switch("qwen")
        a.join(2)
        self.assertEqual(results["glm"]["state"], "switching")
        self.assertEqual(results["qwen"]["state"], "waiting")           # glm's start now holds the GPU
        self.assertEqual(self.started, ["glm"])

    def test_an_adopted_loading_model_is_waited_for_not_restarted(self):
        """A model adopted after a restart is loading with no start job: waiting beats loading it a second time."""
        inst = self.running("glm", "maya", 30)
        inst.state, inst.ready = "starting", None
        res = self.mgr.request_switch("glm")
        self.assertEqual(res["state"], "waiting")
        self.assertIn("loading", res["reason"])
        self.assertEqual(self.started, [])
        self.mgr.jobs["j1"] = job = self.job("glm", "running")       # a start this manager did queue: follow it
        job.start_profile = "glm"
        res = self.mgr.request_switch("glm")
        self.assertEqual(res["state"], "switching")
        self.assertIs(res["job"], job)
        self.assertIs(self.mgr.switch_jobs["glm"], job)
        self.assertEqual(self.started, [])

    def test_a_failed_start_backs_off_and_doubles(self):
        self.mgr.switch_jobs["glm"] = failed = self.job("glm", "failed", "RuntimeError: exited during start")
        failed.finished = time.time()
        res = self.mgr.request_switch("glm")
        self.assertEqual(res["state"], "refused")
        self.assertIn("exited during start", res["reason"])
        self.assertAlmostEqual(res["wait_s"], 15, delta=1)
        self.mgr.switch_backoff["glm"] = (time.time() - 1, "x", 15)       # the wait is over: it tries again
        self.assertEqual(self.mgr.request_switch("glm")["state"], "switching")
        self.mgr.switch_jobs["glm"] = again = self.job("glm", "failed", "RuntimeError: again")
        again.finished = time.time()
        self.assertAlmostEqual(self.mgr.request_switch("glm")["wait_s"], 30, delta=1)

    def test_a_start_that_worked_clears_an_old_failure(self):
        self.mgr.switch_jobs["glm"] = failed = self.job("glm", "failed", "RuntimeError: exited during start")
        failed.finished = time.time()
        self.assertEqual(self.mgr.request_switch("glm")["state"], "refused")   # backing off
        inst = self.running("glm", "maya", 999)                                # then started by hand, and up
        self.assertEqual(self.mgr.request_switch("glm")["state"], "ready")
        del self.mgr.instances[inst.engine]                                    # stopped again later
        self.assertEqual(self.mgr.request_switch("glm")["state"], "switching")

    def test_a_superseded_start_is_not_a_failure(self):
        self.mgr.switch_jobs["glm"] = sup = self.job("glm", "refused", "a newer start of qwen is queued and ...")
        sup.finished = time.time()
        self.assertEqual(self.mgr.request_switch("glm")["state"], "switching")

    def test_an_idle_switched_in_model_returns_home(self):
        self.mgr.set_switching({"home": "qwen", "return_home_minutes": 15})
        inst = self.running("glm", "maya", 3600)
        inst.last_activity = time.time() - 16 * 60
        self.assertTrue(self.mgr._maybe_return_home(inst))
        self.assertEqual(self.started, ["qwen"])
        inst.last_activity = time.time()
        self.started.clear()
        self.assertFalse(self.mgr._maybe_return_home(inst))
        self.assertFalse(self.mgr._maybe_return_home(self.running("qwen", "strata", 3600)))   # home stays home

    def test_return_home_waits_for_a_busy_model(self):
        """The exclusive home start would stop a model generating right now: wait for it instead."""
        self.mgr.set_switching({"home": "qwen", "return_home_minutes": 15})
        self.mgr.profiles["coder"] = Profile("coder", "llamacpp", {}, policy="coexist")
        glm = self.running("glm", "maya", 3600)
        glm.last_activity = time.time() - 16 * 60
        coder = self.running("coder", "llamacpp", 3600)
        coder.last_activity = time.time()                                # generating now
        self.assertFalse(self.mgr._maybe_return_home(glm))
        self.assertEqual(self.started, [])
        coder.last_activity = time.time() - 16 * 60
        self.assertTrue(self.mgr._maybe_return_home(glm))
        self.assertEqual(self.started, ["qwen"])

    def test_a_failing_home_backs_off_instead_of_retrying_every_tick(self):
        self.mgr.set_switching({"home": "qwen", "return_home_minutes": 15})
        inst = self.running("glm", "maya", 3600)
        inst.last_activity = time.time() - 16 * 60
        self.assertTrue(self.mgr._maybe_return_home(inst))
        self.mgr.switch_jobs["qwen"].state, self.mgr.switch_jobs["qwen"].error = "refused", "port 8082 is in use"
        self.mgr.switch_jobs["qwen"].finished = time.time()
        for _ in range(5):                                            # the collector ticks on
            self.assertFalse(self.mgr._maybe_return_home(inst))
        self.assertEqual(self.started, ["qwen"])                     # one try, then the back-off holds

    def test_home_already_up_is_not_restarted(self):
        self.mgr.set_switching({"home": "qwen", "return_home_minutes": 15})
        self.running("qwen", "strata", 600)                             # home is up on its own engine
        glm = self.running("glm", "maya", 3600)
        glm.last_activity = time.time() - 16 * 60                       # switched-in model idle past the return time
        self.assertFalse(self.mgr._maybe_return_home(glm))
        self.assertEqual(self.started, [])


class FakeRoles:
    def __init__(self, targets, switch=None):
        self._targets, self._switch = targets, switch

    def candidates(self, model, need=0, who=None):
        return [dict(t) for t in self._targets]

    def switch_target(self, model):
        return self._switch

    def opened(self, t, switching=False):
        return True

    def closed(self, t):
        pass

    def note_used(self, *a, **k):
        pass

    def public(self):
        return [{"name": "glm"}]

    def wake_for(self, model):
        return []

    def starting(self, model):
        return False

    def stopping(self, model):
        return False

    def all(self):
        return {"glm": {}}


class FakePeers:
    def __init__(self):
        self.state = {name: {"online": True, "seen": time.time(), "status": {"engines": []}} for name in ("pc", "mac")}

    def get(self, name):
        return {"url": PEER_GATEWAY if name == "pc" else "http://10.0.0.8:9000", "token": "peertok"}


class GatewaySwitchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = Path(tempfile.mkdtemp())
        cls.mgr = Manager({"manager": {"host": "127.0.0.1", "port": 9000, "data_dir": d, "logs_dir": d},
                           "resources": {"sample_interval_s": 5}, "engines": {}})
        cls.mgr.profiles["glm-p"] = Profile("glm-p", "maya", {}, description="GLM 5.3")
        cls.mgr.needs = lambda p: {"load_seconds": 75}

    def post(self, client, model="glm", stream=False, headers=None):
        return client.post("/v1/chat/completions", json={"model": model, "max_tokens": 5, "stream": stream,
                                                         "messages": [{"role": "user", "content": "hi"}]},
                           headers={"Authorization": "Bearer testtok", "X-Hangar-Via": "test", "X-Hangar-Caller": "dev",
                                    **(headers or {})})

    def test_hub_relays_a_switching_role_to_its_machine(self):
        seen = []

        def h(req):
            seen.append((str(req.url), req.headers.get("x-hangar-switch"), json.loads(req.content)["model"]))
            return httpx.Response(200, json={"ok": True})
        roles = FakeRoles([{"machine": "mac", "profile": "coder"}], switch={"machine": "pc", "profile": "glm-p"})
        client = TestClient(create_app(self.mgr, peers=FakePeers(), roles=roles,
                                       gateway=httpx.AsyncClient(transport=httpx.MockTransport(h))))
        r = self.post(client)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(seen, [(PEER_GATEWAY + "/v1/chat/completions", "glm", "profile:glm-p")])
        self.assertNotIn("x-hangar-fallback", r.headers)

    def test_a_failed_relay_falls_back_labelled(self):
        def h(req):
            if str(req.url).startswith(PEER_GATEWAY):
                raise httpx.ConnectError("down")
            return httpx.Response(200, json={"ok": True})
        roles = FakeRoles([{"machine": "mac", "profile": "coder"}], switch={"machine": "pc", "profile": "glm-p"})
        client = TestClient(create_app(self.mgr, peers=FakePeers(), roles=roles,
                                       gateway=httpx.AsyncClient(transport=httpx.MockTransport(h))))
        r = self.post(client)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-hangar-served-by"], "mac/coder")
        self.assertEqual(r.headers["x-hangar-fallback"], "pc/glm-p")

    def test_a_too_long_request_falls_back_from_the_switch_target(self):
        """The switch target cannot fit the request: the fallbacks behind it still get their turn."""
        def h(req):
            if str(req.url).startswith(PEER_GATEWAY):
                return httpx.Response(413, json={"error": {"message": "the request exceeds the context size"}})
            return httpx.Response(200, json={"ok": True})
        roles = FakeRoles([{"machine": "mac", "profile": "coder"}], switch={"machine": "pc", "profile": "glm-p"})
        client = TestClient(create_app(self.mgr, peers=FakePeers(), roles=roles,
                                       gateway=httpx.AsyncClient(transport=httpx.MockTransport(h))))
        r = self.post(client)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-hangar-served-by"], "mac/coder")
        self.assertEqual(r.headers["x-hangar-fallback"], "pc/glm-p")

    def test_a_local_role_falls_back_when_its_switch_is_refused(self):
        """Its own model would not start here: the role's other targets answer, labelled as answering instead."""
        self.mgr.request_switch = lambda pid: {"state": "refused", "reason": "backing off", "wait_s": 15}
        roles = FakeRoles([{"machine": "pc", "profile": "backup"}], switch={"machine": "", "profile": "glm-p"})
        client = TestClient(create_app(self.mgr, peers=FakePeers(), roles=roles,
                                       gateway=httpx.AsyncClient(transport=httpx.MockTransport(
                                           lambda req: httpx.Response(200, json={"ok": True})))))
        r = self.post(client)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-hangar-served-by"], "pc/backup")
        self.assertIn("glm-p", r.headers["x-hangar-fallback"])

    def owner(self, answers, upstream=None):
        """This machine owns glm-p: a relayed request switches here. answers: what request_switch says, in turn."""
        calls = iter(answers)
        self.mgr.request_switch = lambda pid: next(calls)
        reply = upstream or httpx.Response(200, text='data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                                                     'data: [DONE]\n\n', headers={"content-type": "text/event-stream"})
        roles = FakeRoles([{"machine": "pc", "profile": "glm-p"}])
        return TestClient(create_app(self.mgr, peers=FakePeers(), roles=roles, gateway=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: reply))))

    def test_a_stream_waits_with_progress_then_answers(self):
        client = self.owner([{"state": "waiting", "wait_s": 0.5, "reason": "qwen keeps the GPU for its minimum turn"},
                             {"state": "ready"}])
        r = self.post(client, model="profile:glm-p", stream=True, headers={"X-Hangar-Switch": "glm"})
        self.assertEqual(r.status_code, 200)
        first, rest = r.text.split("\n\n", 1)
        delta = json.loads(first[len("data: "):])["choices"][0]["delta"]
        self.assertTrue(delta["reasoning_content"].startswith(PROGRESS + "qwen keeps the GPU"))
        self.assertIn('"content":"hello"', rest)
        self.assertEqual(r.headers["x-hangar-switching-to"].split("/")[1], "glm-p")

    def test_a_waiting_request_never_starts_again(self):
        """Its own job is the one it waits on: re-asking would queue a fresh start and supersede a newer one."""
        job = Job(kind="start", title="glm-p", fn=lambda j: None)
        asked = []

        def request_switch(pid):
            asked.append(pid)
            return {"state": "switching", "job": job}

        def superseded():
            job.state = "refused"
            job.error = "a newer start of qwen is queued and would stop glm-p again at once"
            job.done_event.set()

        self.mgr.request_switch = request_switch
        threading.Timer(0.3, superseded).start()
        roles = FakeRoles([{"machine": "pc", "profile": "glm-p"}])
        client = TestClient(create_app(self.mgr, peers=FakePeers(), roles=roles, gateway=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, text="data: [DONE]\n\n")))))
        r = self.post(client, model="profile:glm-p", stream=True, headers={"X-Hangar-Switch": "glm"})
        self.assertIn("newer switch", r.text.rstrip().split("\n\n")[-2])        # the event before [DONE]
        self.assertTrue(r.text.endswith("data: [DONE]\n\n"))
        self.assertEqual(asked, ["glm-p"])

    def test_a_stream_for_a_model_already_up_gets_real_status_codes(self):
        """Nothing had to wait, so no stream wrapper: the engine's status reaches the hub and its fallbacks."""
        client = self.owner([{"state": "ready"}],
                            httpx.Response(413, json={"error": {"message": "the request exceeds the context size"}}))
        r = self.post(client, model="profile:glm-p", stream=True, headers={"X-Hangar-Switch": "glm"})
        self.assertEqual(r.status_code, 413)
        self.assertIn("exceeds the context size", r.text)

    def test_a_stream_refused_at_once_gets_a_503(self):
        """A back-off is known before any header goes out: answer it as a plain 503 a hub can fall back from."""
        client = self.owner([{"state": "refused", "reason": "backing off", "wait_s": 15}])
        r = self.post(client, model="profile:glm-p", stream=True, headers={"X-Hangar-Switch": "glm"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("retry-after", r.headers)

    def test_an_engine_error_after_the_switch_is_a_stream_error(self):
        """The wait has begun, so this engine error can only be reported inside the stream."""
        client = self.owner([{"state": "waiting", "wait_s": 0.5, "reason": "qwen keeps the GPU"}, {"state": "ready"}],
                            httpx.Response(400, json={"error": {"message": "bad sampling"}}))
        r = self.post(client, model="profile:glm-p", stream=True, headers={"X-Hangar-Switch": "glm"})
        self.assertIn("bad sampling", r.text)
        self.assertTrue(r.text.endswith("data: [DONE]\n\n"))

    def test_a_refused_switch_ends_the_stream_with_its_reason(self):
        """Refused once the stream is open: its reason goes into the stream, and the stream ends."""
        client = self.owner([{"state": "waiting", "wait_s": 0.5, "reason": "x keeps the GPU"},
                             {"state": "refused", "reason": "glm-p did not start (out of memory); trying again in 15 s",
                              "wait_s": 15}])
        r = self.post(client, model="profile:glm-p", stream=True, headers={"X-Hangar-Switch": "glm"})
        self.assertIn("out of memory", r.text)
        self.assertTrue(r.text.endswith("data: [DONE]\n\n"))

    def test_a_local_streaming_role_falls_back_after_progress(self):
        """The switch was refused once the stream was open: this role's own backup still gets its turn."""
        calls = iter([{"state": "waiting", "wait_s": 0.5, "reason": "x keeps the GPU"},
                      {"state": "refused", "reason": "backing off", "wait_s": 15}])
        self.mgr.request_switch = lambda pid: next(calls)
        reply = httpx.Response(200, text='data: {"choices":[{"delta":{"content":"from backup"}}]}\n\n'
                                         'data: [DONE]\n\n', headers={"content-type": "text/event-stream"})
        roles = FakeRoles([{"machine": "pc", "profile": "backup"}], switch={"machine": "", "profile": "glm-p"})
        client = TestClient(create_app(self.mgr, peers=FakePeers(), roles=roles,
                                       gateway=httpx.AsyncClient(transport=httpx.MockTransport(lambda req: reply))))
        r = self.post(client, model="glm", stream=True)
        self.assertIn("from backup", r.text)
        self.assertNotIn("backing off", r.text)

    def test_a_plain_request_waits_then_answers_or_gets_503(self):
        client = self.owner([{"state": "ready"}])
        self.assertEqual(self.post(client, model="profile:glm-p", headers={"X-Hangar-Switch": "glm"}).status_code, 200)
        client = self.owner([{"state": "refused", "reason": "no", "wait_s": 15}])
        r = self.post(client, model="profile:glm-p", headers={"X-Hangar-Switch": "glm"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("retry-after", r.headers)

    def test_without_the_switch_header_a_profile_is_not_switched_to(self):
        client = self.owner([])                                         # request_switch must not be asked
        self.assertEqual(self.post(client, model="profile:glm-p").status_code, 200)

    def test_warming_a_role_nothing_can_serve_says_so(self):
        roles = FakeRoles([])                                          # its machine is down and nothing else is up
        roles.wake_for = lambda model: ["pc"]
        client = TestClient(create_app(self.mgr, peers=FakePeers(), roles=roles, gateway=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: self.fail(f"called {req.url}")))))
        r = client.post("/v1/hangar/warm", json={"model": "glm"}, headers={"Authorization": "Bearer testtok"})
        self.assertEqual(r.json()["state"], "waking")
        roles.wake_for = lambda model: []
        r = client.post("/v1/hangar/warm", json={"model": "glm"}, headers={"Authorization": "Bearer testtok"})
        self.assertEqual(r.json()["state"], "unavailable")

    def test_warm_is_also_under_api(self):
        """hangar --on <machine> forwards only /api/... calls, so warming a peer needs the /api path too."""
        roles = FakeRoles([])
        roles.wake_for = lambda model: ["pc"]
        client = TestClient(create_app(self.mgr, peers=FakePeers(), roles=roles, gateway=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: self.fail(f"called {req.url}")))))
        r = client.post("/api/warm", json={"model": "glm"}, headers={"Authorization": "Bearer testtok"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), client.post("/v1/hangar/warm", json={"model": "glm"},
                                               headers={"Authorization": "Bearer testtok"}).json())
        self.assertEqual(r.json()["state"], "waking")

    def test_warming_a_role_whose_profile_was_removed_is_unavailable(self):
        r = roles_with({"targets": [{"machine": "", "profile": "gone"}], "switch": True})
        r.wake_for = lambda model: []
        client = TestClient(create_app(self.mgr, peers=FakePeers(), roles=r, gateway=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: self.fail(f"called {req.url}")))))
        res = client.post("/v1/hangar/warm", json={"model": "glm"}, headers={"Authorization": "Bearer testtok"})
        self.assertEqual(res.json()["state"], "unavailable")

    def test_warming_a_role_never_starts_a_same_named_profile(self):
        """A role and a stopped local profile share the name: warming the role must not switch to the profile."""
        self.mgr.profiles["coder"] = Profile("coder", "strata", {})
        self.addCleanup(self.mgr.profiles.pop, "coder", None)
        switch = self.mgr.request_switch
        self.addCleanup(setattr, self.mgr, "request_switch", switch)
        self.mgr.request_switch = lambda pid: self.fail(f"warming a role switched to the profile {pid!r}")

        roles = FakeRoles([{"machine": "mac", "profile": "glm-p"}])     # switch_target: None, a role called coder
        roles.all = lambda: {"coder": {}}
        client = TestClient(create_app(self.mgr, peers=FakePeers(), roles=roles, gateway=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True})))))
        r = client.post("/v1/hangar/warm", json={"model": "coder"}, headers={"Authorization": "Bearer testtok"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "ready")


if __name__ == "__main__":
    unittest.main()


class ClientRolesTest(unittest.TestCase):
    """The clients' own plan/build/small slots point at the switching roles, merged into what is already there."""
    MAPPING = {"plan": "glm", "build": "qwen", "small": "helper"}

    def entries(self):
        return [{"profile": f"role-{n}", "engine": "hangar", "provider": "hangar", "provider_name": "Hangar roles",
                 "base": "http://hub:9000", "api_key": "K", "model": n, "name": n, "context": 131072, "output": 16384,
                 "vision": False, "reasoning": True, "env_key": "HANGAR_TOKEN"} for n in ("glm", "qwen", "helper")]

    def test_opencode_agents_and_small_model(self):
        from manager import clients
        cfg = json.loads(clients.with_roles(clients.gen_opencode(self.entries(), False), "opencode", self.MAPPING)["content"])
        self.assertEqual(cfg["agent"], {"plan": {"model": "hangar/glm"}, "build": {"model": "hangar/qwen"}})
        self.assertEqual((cfg["model"], cfg["small_model"]), ("hangar/qwen", "hangar/helper"))

    def test_omp_roles_keep_side_work_off_the_switching_models(self):
        from manager import clients
        snip = clients.with_roles(clients.gen_omp(self.entries(), False), "omp", self.MAPPING)
        self.assertEqual(snip["model_roles"], {"default": "hangar/qwen", "plan": "hangar/glm", "slow": "hangar/glm",
                                               "smol": "hangar/helper", "task": "hangar/helper", "commit": "hangar/helper"})

    def test_no_mapping_changes_nothing(self):
        from manager import clients
        snip = clients.gen_opencode(self.entries(), False)
        self.assertIs(clients.with_roles(snip, "opencode", {}), snip)

    def test_applying_merges_without_clobbering(self):
        import yaml
        from manager import clients
        d = Path(tempfile.mkdtemp())
        oc = d / "opencode.json"
        oc.write_text(json.dumps({"small_model": "mine/fast", "agent": {"plan": {"prompt": "keep me", "model": "x/y"}},
                                  "provider": {"other": {"models": {}}}}))
        snip = clients.with_roles(clients.gen_opencode(self.entries(), False), "opencode", self.MAPPING)
        clients.apply("opencode", {**snip, "file": str(oc)})
        cur = json.loads(oc.read_text())
        self.assertEqual(cur["agent"]["plan"], {"prompt": "keep me", "model": "hangar/glm"})
        self.assertEqual(cur["small_model"], "mine/fast")                  # the user's own choice stays
        self.assertIn("other", cur["provider"])
        models = d / "models.yml"
        (d / "config.yml").write_text(yaml.safe_dump({"theme": {"dark": "titanium"}, "modelRoles": {"vision": "x/v"}}))
        snip = clients.with_roles(clients.gen_omp(self.entries(), False), "omp", self.MAPPING)
        clients.apply("omp", {**snip, "file": str(models)})
        cfg = yaml.safe_load((d / "config.yml").read_text())
        self.assertEqual(cfg["theme"], {"dark": "titanium"})
        self.assertEqual(cfg["modelRoles"]["vision"], "x/v")
        self.assertEqual(cfg["modelRoles"]["plan"], "hangar/glm")


class SetupTest(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.mgr = mgr = Manager({"manager": {"host": "127.0.0.1", "port": 9000, "data_dir": d, "logs_dir": d},
                                  "resources": {"sample_interval_s": 5}, "engines": {}})
        mgr.profiles.update({"glm-p": Profile("glm-p", "maya", {}), "qwen-p": Profile("qwen-p", "strata", {})})
        self.roles = Roles(mgr, SimpleNamespace(state={}, all=lambda: {}))
        self.client = TestClient(create_app(mgr, peers=SimpleNamespace(state={}, all=lambda: {}, get=lambda n: {}),
                                            roles=self.roles))

    def post(self, body):
        return self.client.post("/api/switching/setup", json=body, headers={"Authorization": "Bearer testtok"})

    def test_one_action_sets_everything_up(self):
        r = self.post({"thinker": "glm-p", "coder": "qwen-p"})
        self.assertEqual(r.status_code, 200, r.text)
        saved = self.mgr._settings()
        self.assertEqual(saved["roles"]["glm"], {"targets": [{"machine": "", "profile": "glm-p", "auto_start": False}],
                                                 "switch": True})
        self.assertTrue(saved["roles"]["qwen"]["switch"])
        self.assertEqual(saved["roles"]["main"]["targets"][0]["profile"], "qwen-p")
        self.assertFalse(saved["roles"]["main"].get("switch"))                 # main never switches on its own
        self.assertIs(saved["main_follows_running"], False)
        self.assertEqual(self.mgr.switching()["home"], "qwen-p")
        self.assertEqual(saved["switching_roles"], {"plan": "glm", "build": "qwen", "small": "helper"})

    def test_role_names_are_normalised_once(self):
        r = self.post({"thinker": "glm-p", "coder": "qwen-p", "thinker_role": " GLM ", "coder_role": "Qwen"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.mgr._settings()["switching_roles"], {"plan": "glm", "build": "qwen", "small": "helper"})

    def test_main_cannot_be_a_switching_role(self):
        for body in ({"thinker_role": "main"}, {"coder_role": "Main"}):
            self.assertEqual(self.post({"thinker": "glm-p", "coder": "qwen-p", **body}).status_code, 400)

    def test_the_small_role_never_switches(self):
        self.assertEqual(self.post({"thinker": "glm-p", "coder": "qwen-p", "small_role": "qwen"}).status_code, 400)
        self.roles.save("fast", [{"profile": "qwen-p"}], switch=True)
        self.assertEqual(self.post({"thinker": "glm-p", "coder": "qwen-p", "small_role": "fast"}).status_code, 400)

    def test_bad_input_is_a_400(self):
        self.assertEqual(self.post({"thinker": "qwen-p", "coder": "qwen-p"}).status_code, 400)
        self.assertEqual(self.post({"thinker": "nope", "coder": "qwen-p"}).status_code, 400)
        self.roles._peer = lambda *a, **k: SimpleNamespace(json=lambda: [{"id": "qwen-p"}])
        self.assertEqual(self.post({"machine": "pc", "thinker": "glm-p", "coder": "qwen-p"}).status_code, 400)

    def test_a_rejected_setup_changes_nothing(self):
        """Nothing is written until every name and profile checks out, so a 400 leaves the routing alone."""
        self.roles.save("glm", [{"profile": "qwen-p"}])
        before = self.mgr._settings()
        self.assertEqual(self.post({"thinker": "glm-p", "coder": "nope"}).status_code, 400)
        self.assertEqual(self.mgr._settings(), before)

    def test_setup_turns_off_switching_on_main(self):
        self.roles.save("main", [{"profile": "qwen-p"}], switch=True)
        self.assertEqual(self.post({"thinker": "glm-p", "coder": "qwen-p"}).status_code, 200)
        self.assertFalse(self.mgr._settings()["roles"]["main"].get("switch"))


class CliSwitchTest(unittest.TestCase):
    """hangar switch <role> follows the switch through and says by its exit status whether the model came up."""
    def run_switch(self, warm_answers, target_states):
        from manager import cli
        warm, states = iter(warm_answers), iter(target_states)

        def call(method, path, **kw):
            if path == "/api/warm":
                return next(warm)
            return [{"name": "glm", "targets": [{"state": next(states, "stopped")}]}]
        with patch.object(cli, "_call", call), patch.object(cli.time, "sleep", lambda s: None), \
                patch("builtins.print"):
            cli.cmd_switch(["glm"])

    def test_switching_then_ready_is_success(self):
        self.run_switch([{"state": "switching"}], ["stopped", "starting", "starting", "ready"])

    def test_refused_is_a_failure(self):
        with self.assertRaises(SystemExit) as e:
            self.run_switch([{"state": "refused", "reason": "backing off"}], [])
        self.assertEqual(e.exception.code, 1)

    def test_a_start_that_ends_without_the_model_is_a_failure(self):
        clock = iter(range(0, 10_000, 30))                                 # each look at the clock: 30 s later
        with patch("manager.cli.time.monotonic", lambda: next(clock)), self.assertRaises(SystemExit):
            self.run_switch([{"state": "switching"}, {"state": "refused", "reason": "out of memory"}],
                            ["starting", "stopped", "stopped", "stopped"])
