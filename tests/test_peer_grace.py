import threading
import time
import unittest
from types import SimpleNamespace

from manager.roles import Roles, PEER_DIRECT_GRACE_S


def _roles(state):
    """A Roles with just enough for the target-state / routing logic: peer state and empty local instances."""
    r = Roles.__new__(Roles)
    r.lock = threading.RLock()
    r.pending = {}
    r.inflight = {}
    r.peers = SimpleNamespace(state=state)
    r.mgr = SimpleNamespace(instances={}, profiles=set())
    return r


def _status(profile, state="ready"):
    return {"engines": [{"port": 8082, "running": {"profile": profile, "state": state}}]}


class PeerGraceTest(unittest.TestCase):
    T = {"machine": "pc", "profile": "m"}

    def test_online_peer_is_ready(self):
        r = _roles({"pc": {"online": True, "status": _status("m"), "seen": time.time()}})
        self.assertEqual(r._target_state(self.T), "ready")
        self.assertTrue(r.routable(self.T))

    def test_gateway_down_within_grace_is_offline_but_routable(self):
        # the manager is restarting (gateway poll failing) but the engine was up the last time we saw it:
        # health says offline (so backup auto-start and wake-on-LAN still fire), but routing keeps going to the engine
        r = _roles({"pc": {"online": False, "status": _status("m"), "seen": time.time() - 10}})
        self.assertEqual(r._target_state(self.T), "offline")
        self.assertTrue(r.routable(self.T))
        self.assertTrue(r.opened(self.T))          # routing proceeds; the request then goes straight to the engine

    def test_peer_beyond_grace_is_offline_and_not_routable(self):
        # been unreachable longer than the grace window: a real reboot - stop routing there
        r = _roles({"pc": {"online": False, "status": _status("m"), "seen": time.time() - PEER_DIRECT_GRACE_S - 1}})
        self.assertEqual(r._target_state(self.T), "offline")
        self.assertFalse(r.routable(self.T))
        self.assertFalse(r.opened(self.T))

    def test_offline_peer_that_was_never_running_is_not_routable(self):
        r = _roles({"pc": {"online": False, "status": _status("other"), "seen": time.time()}})
        self.assertEqual(r._target_state(self.T), "offline")
        self.assertFalse(r.routable(self.T))

    def test_never_seen_offline_peer_is_not_routable(self):
        r = _roles({"pc": {"online": False, "status": _status("m")}})   # no 'seen'
        self.assertFalse(r.routable(self.T))

    def test_online_peer_without_the_profile_is_stopped_not_offline(self):
        r = _roles({"pc": {"online": True, "status": _status("other"), "seen": time.time()}})
        self.assertEqual(r._target_state(self.T), "stopped")


if __name__ == "__main__":
    unittest.main()
