"""Print the bodies the identity routes return, for the browser tests' stub check.

The two routes need a face service and a GPU workflow, which the fixture gateway
cannot run. Their handlers are real here: this builds them over the same fakes that
tests/test_identity_api.py uses and prints one successful body per route as JSON,
keyed by the stub registry's endpoint id (web/e2e/fixtures/oracle.ts reads it).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import test_identity_api as fixtures  # noqa: E402

client, _, _ = fixtures.build([fixtures.GOOD])
likeness = client.post(
    fixtures.identity_api.LIKENESS_PATH,
    files=[("reference", ("p.png", fixtures.PHOTO, "image/png")), ("candidate", ("c.png", fixtures.GOOD, "image/png"))],
)
scene = client.post(
    fixtures.identity_api.IDENTITY_PATH,
    files=[("image", ("p.png", fixtures.PHOTO, "image/png"))],
    data={"prompt": "They look into each other's eyes."},
)
for response in (likeness, scene):
    if response.status_code != 200:
        raise SystemExit(f"identity route returned HTTP {response.status_code}: {response.text[:200]}")
print(json.dumps({
    "POST /gateway/v1/faces/likeness": likeness.json(),
    "POST /gateway/v1/images/identity": scene.json(),
}))
