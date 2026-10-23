"""Routing to a peer engine while that peer's manager is offline: direct engine health decides, with no time limit
(the fixed grace window was dropped in 0.3.0)."""
import threading
import time
import unittest
from types import SimpleNamespace

from manager.roles import Roles


def _roles(state):
    """A Roles with just enough for the target-state / routing logic: peer state and empty local instances."""
    r = Roles.__new__(Roles)
    r.lock = threading.RLock()
    r.pending = {}
    r.inflight = {}
    r.peers = SimpleNamespace(state=state)
    r.mgr = SimpleNamespace(instances={}, profiles=set())
    return r


def _status(profile, state="ready", port=8082, pid=111, started=1000.0):
    return {"engines": [{"port": port, "running": {"profile": profile, "state": state, "pid": pid, "started": started}}]}


def _healthy(profile, port=8082, pid=111, started=1000.0, online=True):
    """The direct engine health this manager last recorded for that peer profile."""
    return {"direct": {profile: {"online": online, "error": None, "checked": time.time(), "instance": (port, pid, started)}}}


class PeerRoutingTest(unittest.TestCase):
    T = {"machine": "pc", "profile": "m"}

    def test_online_peer_is_ready(self):
        r = _roles({"pc": {"online": True, "status": _status("m")}})
        self.assertEqual(r._target_state(self.T), "ready")
        self.assertTrue(r.routable(self.T))
        self.assertTrue(r.opened(self.T))          # routing proceeds through its manager

    def test_manager_down_but_engine_verified_stays_routable(self):
        # the manager is restarting (its gateway poll failed) but the engine answers directly: keep routing to it
        r = _roles({"pc": {"online": False, "status": _status("m"), "seen": time.time() - 10, **_healthy("m")}})
        self.assertEqual(r._target_state(self.T), "ready")
        self.assertTrue(r.routable(self.T))
        self.assertTrue(r.opened(self.T))          # routing proceeds; the request goes straight to the engine

    def test_verified_engine_stays_routable_whatever_the_manager_has_been_down(self):
        # no fixed grace window: how long the manager has been unreachable does not matter, only the engine's health
        r = _roles({"pc": {"online": False, "status": _status("m"), "seen": time.time() - 86400, **_healthy("m")}})
        self.assertEqual(r._target_state(self.T), "ready")
        self.assertTrue(r.routable(self.T))

    def test_verified_engine_that_stopped_answering_is_not_routable(self):
        r = _roles({"pc": {"online": False, "status": _status("m"), "seen": time.time(), **_healthy("m", online=False)}})
        self.assertEqual(r._target_state(self.T), "offline")
        self.assertFalse(r.routable(self.T))
        self.assertFalse(r.opened(self.T))

    def test_offline_peer_without_direct_health_is_not_routable(self):
        r = _roles({"pc": {"online": False, "status": _status("m"), "seen": time.time()}})
        self.assertEqual(r._target_state(self.T), "offline")
        self.assertFalse(r.routable(self.T))
        self.assertFalse(r.opened(self.T))

    def test_direct_health_of_another_instance_does_not_count(self):
        # the peer restarted the engine in between: the old probe says nothing about the new process
        r = _roles({"pc": {"online": False, "status": _status("m", pid=222), "seen": time.time(),
                           **_healthy("m", pid=111)}})
        self.assertEqual(r._target_state(self.T), "offline")
        self.assertFalse(r.routable(self.T))

    def test_offline_peer_that_was_never_running_is_not_routable(self):
        r = _roles({"pc": {"online": False, "status": _status("other"), "seen": time.time(), **_healthy("m")}})
        self.assertEqual(r._target_state(self.T), "offline")
        self.assertFalse(r.routable(self.T))

    def test_online_peer_without_the_profile_is_stopped_not_offline(self):
        r = _roles({"pc": {"online": True, "status": _status("other"), "seen": time.time()}})
        self.assertEqual(r._target_state(self.T), "stopped")

    def test_online_peer_with_an_unhealthy_engine_is_offline(self):
        # the manager is up and reports the profile running, but the direct probe says it does not answer
        r = _roles({"pc": {"online": True, "status": _status("m"), "seen": time.time(), **_healthy("m", online=False)}})
        self.assertEqual(r._target_state(self.T), "offline")
        self.assertFalse(r.routable(self.T))


if __name__ == "__main__":
    unittest.main()
