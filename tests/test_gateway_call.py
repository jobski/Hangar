"""gateway_call routing: the relay path that broke in production (a stray except turned every answer into a 502).

These drive the REAL gateway_call handler with stub roles/peers and a mock transport, pinning each branch:
a healthy peer gateway still relays (the path that regressed); a gateway that is refusing/404/5xx, or a peer in its
restart grace, falls back to that peer's engine straight - authenticated with the engine key it published."""
import os
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from manager.api import create_app
from manager.core import Manager

os.environ["LLM_MANAGER_TOKEN"] = "testtok"

PEER_ENGINE = "http://10.0.0.9:8082"
PEER_GATEWAY = "http://10.0.0.9:9000"
ENGINE_KEY = "engine-secret"
TARGET = {"machine": "pc", "profile": "m"}


class FakePeers:
    def __init__(self, online=True, profile_ready=True, seen=0):
        import time
        engines = []
        if profile_ready:
            engines = [{"name": "strata", "port": 8082, "plugin": "strata",
                        "running": {"profile": "m", "state": "ready", "engine_key": ENGINE_KEY}}]
        self.state = {"pc": {"online": online, "seen": time.time() - seen,
                             "status": {"engines": engines}}}
        self._peers = {"pc": {"url": PEER_GATEWAY, "token": "peertok"}}

    def get(self, name):
        return self._peers[name]


class FakeRoles:
    def __init__(self, targets):
        self._targets = targets

    def candidates(self, model, need=0, who=None):
        return [dict(t) for t in self._targets]

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

    def stopping(self, model):
        return False


def engine_ok(req):
    """A mock response standing in for a peer's engine: /v1/models lists one model, generation returns a body."""
    if str(req.url).endswith("/v1/models"):
        return httpx.Response(200, json={"data": [{"id": "strata-orca"}]})
    return httpx.Response(200, json={"ok": True})

class GatewayCallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = Path(tempfile.mkdtemp())
        cls.mgr = Manager({"manager": {"host": "127.0.0.1", "port": 9000, "data_dir": d, "logs_dir": d},
                           "resources": {"sample_interval_s": 5}, "engines": {}})

    def _client(self, peers, gateway):
        return TestClient(create_app(self.mgr, peers=peers, roles=FakeRoles([TARGET]), gateway=gateway))

    def _post(self, client):
        return client.post("/v1/chat/completions", json={"model": "main", "max_tokens": 5},
                           headers={"Authorization": "Bearer testtok",
                                    "X-Hangar-Via": "test", "X-Hangar-Caller": "dev"})

    def test_healthy_peer_gateway_relays(self):
        # the path that regressed: an online peer's gateway answers -> relayed straight back, no direct-route marker
        seen = []
        def h(req):
            seen.append((str(req.url), req.headers.get("authorization")))
            return httpx.Response(200, json={"ok": True})
        client = self._client(FakePeers(online=True), httpx.AsyncClient(transport=httpx.MockTransport(h)))
        r = self._post(client)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-hangar-served-by"], "pc/m")
        self.assertNotIn("x-hangar-route", r.headers)
        self.assertEqual(seen, [(PEER_GATEWAY + "/v1/chat/completions", "Bearer peertok")])   # only the gateway, not the engine

    def test_onset_connect_error_falls_back_to_engine(self):
        # online still cached, but the gateway is already refusing (its manager is on its way out): go straight to the engine
        called = []
        def h(req):
            called.append(str(req.url))
            if req.url.port == 9000:
                raise httpx.ConnectError("refused")
            return engine_ok(req)
        client = self._client(FakePeers(online=True), httpx.AsyncClient(transport=httpx.MockTransport(h)))
        r = self._post(client)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-hangar-route"], "direct")
        self.assertEqual(r.headers["x-hangar-served-by"], "pc/m")
        self.assertEqual(called, [PEER_GATEWAY + "/v1/chat/completions",      # gateway refused first
                                  PEER_ENGINE + "/v1/models", PEER_ENGINE + "/v1/chat/completions"])  # then engine

    def test_engine_request_carries_published_key(self):
        # the direct engine call must authenticate with the engine key the peer published, not the peer token
        rec = []
        def h(req):
            rec.append(req.headers.get("authorization"))
            if req.url.port == 9000:
                raise httpx.ConnectError("refused")
            return engine_ok(req)
        client = self._client(FakePeers(online=True), httpx.AsyncClient(transport=httpx.MockTransport(h)))
        r = self._post(client)
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"Bearer {ENGINE_KEY}", rec)

    def test_old_peer_404_falls_back_to_engine(self):
        def h(req):
            if req.url.port == 9000:
                return httpx.Response(404, text="nope")
            return engine_ok(req)
        client = self._client(FakePeers(online=True), httpx.AsyncClient(transport=httpx.MockTransport(h)))
        r = self._post(client)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-hangar-route"], "direct")

    def test_gateway_down_in_grace_uses_engine(self):
        # offline (poll flipped it) but within grace with a last-known-ready engine: straight to the engine, no gateway try
        called = []
        def h(req):
            called.append(str(req.url))
            return engine_ok(req)
        client = self._client(FakePeers(online=False, seen=10), httpx.AsyncClient(transport=httpx.MockTransport(h)))
        r = self._post(client)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-hangar-route"], "direct")
        self.assertEqual(called, [PEER_ENGINE + "/v1/models", PEER_ENGINE + "/v1/chat/completions"])  # engine only

    def test_engine_probe_cached_across_requests(self):
        # the model id is probed once, not on every relayed request
        probes = []
        def h(req):
            if str(req.url).endswith("/v1/models"):
                probes.append(str(req.url))
            return httpx.Response(200, json={"data": [{"id": "strata-orca"}]})
        client = self._client(FakePeers(online=False, seen=10), httpx.AsyncClient(transport=httpx.MockTransport(h)))
        for _ in range(3):
            self.assertEqual(self._post(client).status_code, 200)
        self.assertEqual(len(probes), 1)

    def test_gateway_5xx_falls_back_to_engine(self):
        def h(req):
            if req.url.port == 9000:
                return httpx.Response(503, text="down")
            return engine_ok(req)
        client = self._client(FakePeers(online=True), httpx.AsyncClient(transport=httpx.MockTransport(h)))
        r = self._post(client)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-hangar-route"], "direct")

    def test_engine_down_after_gateway_fails_502(self):
        # gateway refuses and the engine is unreachable too -> every target failed
        def h(req):
            raise httpx.ConnectError("refused")
        client = self._client(FakePeers(online=True), httpx.AsyncClient(transport=httpx.MockTransport(h)))
        r = self._post(client)
        self.assertEqual(r.status_code, 502)

    def test_nothing_up_returns_503(self):
        # no target routable (roles has none) -> 503 'nothing is up', never a relay attempt
        client = TestClient(create_app(self.mgr, peers=FakePeers(online=True), roles=FakeRoles([]),
                                       gateway=httpx.AsyncClient(transport=httpx.MockTransport(
                                           lambda req: httpx.Response(200, json={"ok": True})))))
        r = self._post(client)
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
