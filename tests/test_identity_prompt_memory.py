"""Native identity output uses the same maker-only prompt policy as other media."""
import io
import json

from PIL import Image

import proxy
from gateway import admin_store, media_jobs, prompt_memory
from tests.support.access import auth_identity
from tests.test_identity_jobs import StubRequest


def test_identity_keeps_only_the_makers_policy_controlled_prompt(tmp_path, monkeypatch):
    admin = tmp_path / "admin.db"
    jobs = tmp_path / "jobs.db"
    admin_store.init_db(admin)
    media_jobs.init_db(jobs)
    maker = admin_store.upsert_oidc_user("test", "maker", path=admin)
    other = admin_store.upsert_oidc_user("test", "other", path=admin)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin)
    monkeypatch.setattr(media_jobs, "DB_PATH", jobs)
    monkeypatch.setattr(prompt_memory, "DB_PATH", jobs)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "images")
    monkeypatch.setattr(proxy, "_image_url_signing_secret", b"fixture-signing-key")
    auth = proxy.AuthContext(identity=auth_identity(
        name="Try", app_id="app_fixture", key_hash="sha256:fixture", person_id=maker["id"]))
    for mode, color in (("remember", "red"), ("forget", "blue")):
        marker = "IDENTITY_PRIVATE_PROMPT_" + mode
        body = {"prompt": marker}
        if mode == "forget":
            body["extensions"] = {prompt_memory.NAMESPACE: {"mode": "forget"}}
        assert proxy._identity_note_delivery(auth, body) is None
        image = io.BytesIO()
        Image.new("RGB", (2, 2), color).save(image, format="PNG")
        item = proxy._identity_store_image(StubRequest(), auth, image.getvalue(), "b64_json")
        saved = prompt_memory.get(item["asset_id"], maker["id"], path=jobs)
        if mode == "remember":
            assert saved["prompt_text"] == marker
        else:
            assert saved is None
        assert prompt_memory.get(item["asset_id"], other["id"], path=jobs) is None
        assert marker not in json.dumps(item)
        assert all(marker.encode() not in path.read_bytes() for path in (tmp_path / "images").iterdir())
