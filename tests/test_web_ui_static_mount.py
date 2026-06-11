"""The static React export (web/out) is an optional build artifact.

The gateway mounts it at /ui (docs/design/WEB_UI_STEERING.md strangler
cutover) ONLY when the directory exists, so a source checkout that never ran
`next build` must boot cleanly — importing proxy is the boot path that runs
the mount guard.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402


class WebUIStaticMountTests(unittest.TestCase):
    def test_missing_export_is_skipped_not_fatal(self) -> None:
        """No web/out → no mount, no exception (gateway boots without it)."""
        bare = FastAPI()
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "web" / "out"  # never created
            self.assertFalse(proxy.mount_web_ui(bare, missing))
        client = TestClient(bare)
        self.assertEqual(client.get("/ui/").status_code, 404)

    def test_export_is_served_when_present(self) -> None:
        bare = FastAPI()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "index.html").write_text(
                "<!doctype html><h1>gateway ui</h1>", encoding="utf-8"
            )
            self.assertTrue(proxy.mount_web_ui(bare, out))
            client = TestClient(bare)
            response = client.get("/ui/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("gateway ui", response.text)

    def test_import_time_guard_matches_checkout_state(self) -> None:
        """Importing proxy already exercised the guard: mounted iff the
        export directory exists. Either way, the import succeeded."""
        self.assertEqual(proxy.WEB_UI_MOUNTED, proxy.WEB_UI_DIR.is_dir())


if __name__ == "__main__":
    unittest.main()
