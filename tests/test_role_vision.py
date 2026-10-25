"""What a client is told about the model behind a role: a role whose model has a vision projector must be offered
with image input, or a client such as opencode refuses attachments on it even though the model can read them.

These pin the generated client config (the copy-paste snippet), not the helper internals, because that config is
what a client acts on."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from manager.api import create_app
from manager.core import Manager
from manager.engines import create
from manager.engines.base import Profile
from manager.roles import Roles

os.environ["LLM_MANAGER_TOKEN"] = "testtok"


class FakePeers:
    """One peer running a vision profile (its engine publishes the vision tag), and its model stopped otherwise."""

    def __init__(self):
        self.state = {"mac": {"online": True, "status": {"engines": [
            {"name": "llamacpp", "port": 8081, "plugin": "llamacpp",
             "running": {"profile": "qwen-v", "state": "ready", "context": 131072, "vision": True}}]}}}

    def all(self):
        return {"mac": {"name": "mac", "url": "http://10.0.0.5:9000", "token": "peertok"}}

    def get(self, name):
        return {"url": "http://10.0.0.5:9000", "token": "peertok"}

    def start(self):
        pass

    def shutdown(self):
        pass


class QuietRoles(Roles):
    """Roles with its background loops left off: TestClient's lifespan would start them."""

    def start(self):
        pass

    def shutdown(self):
        pass


class RoleVisionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        d = Path(cls.tmp.name)
        cls.mgr = mgr = Manager({"manager": {"host": "127.0.0.1", "port": 9000, "data_dir": d, "logs_dir": d},
                                 "resources": {"sample_interval_s": 5}, "engines": {}})
        mgr.engines["llamacpp"] = create("llamacpp", {"plugin": "llamacpp", "dir": str(d), "port": 8081,
                                                      "_data_dir": str(d)})
        mgr.profiles.update({
            "vision-model": Profile("vision-model", "llamacpp", {"mmproj": str(d / "mmproj.gguf"), "ctx": 131072}),
            "text-model": Profile("text-model", "llamacpp", {"ctx": 32768})})
        mgr.refresh_profiles = lambda **k: None
        roles = QuietRoles(mgr, FakePeers())
        roles.save("helper", [{"machine": "", "profile": "vision-model"}])
        roles.save("plain", [{"machine": "", "profile": "text-model"}])
        roles.save("remote", [{"machine": "mac", "profile": "qwen-v"}])
        roles.save("asleep", [{"machine": "mac", "profile": "gone-model"}])
        cls.client = TestClient(create_app(mgr, peers=FakePeers(), roles=roles))

    def role_models(self):
        r = self.client.get("/api/connect/models", params={"client": "opencode", "host": "10.0.0.1"},
                            headers={"Authorization": "Bearer testtok"})
        self.assertEqual(r.status_code, 200)
        return json.loads(r.json()["content"])["provider"]["hangar"]["models"]

    def test_a_role_on_a_vision_profile_is_offered_with_images(self):
        model = self.role_models()["helper"]
        self.assertTrue(model["attachment"])
        self.assertEqual(model["modalities"]["input"], ["text", "image"])

    def test_a_role_on_a_text_only_profile_is_not(self):
        self.assertNotIn("attachment", self.role_models()["plain"])

    def test_a_vision_profile_on_a_peer_carries_its_tag_to_this_hub(self):
        self.assertTrue(self.role_models()["remote"]["attachment"])

    def test_the_capability_follows_the_model_the_role_is_set_to_not_the_one_answering(self):
        # gone-model is down, so the peer's running qwen-v answers for it: the offer still describes gone-model,
        # whose projector this hub cannot see. A client must not be promised images by a stand-in.
        r = self.client.get("/api/roles", headers={"Authorization": "Bearer testtok"}).json()
        asleep = next(x for x in r if x["name"] == "asleep")
        self.assertEqual((asleep["serving"]["machine"], asleep["serving"]["profile"]), ("mac", "qwen-v"))
        self.assertTrue(asleep["fallback"])
        self.assertNotIn("attachment", self.role_models()["asleep"])


if __name__ == "__main__":
    unittest.main()
