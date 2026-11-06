"""The public setup contract exposes capabilities, never account or secret data."""

import sys
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.setup_api import build_setup_api_router  # noqa: E402


class SetupApiTests(unittest.TestCase):
    def test_public_setup_state_is_secret_free_and_uncached(self):
        app = FastAPI()
        app.include_router(build_setup_api_router(lambda request: {
            "owner_exists": True,
            "oidc_enabled": False,
            "can_recover": True,
            "email": "private@example.test",
            "signing_key": "private",
        }))
        response = TestClient(app).get("/setup/api/state")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "owner_exists": True,
            "oidc_enabled": False,
            "can_recover": True,
        })
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_first_run_capabilities_default_to_false(self):
        app = FastAPI()
        app.include_router(build_setup_api_router(lambda request: {}))
        self.assertEqual(TestClient(app).get("/setup/api/state").json(), {
            "owner_exists": False,
            "oidc_enabled": False,
            "can_recover": False,
        })


if __name__ == "__main__":
    unittest.main()
