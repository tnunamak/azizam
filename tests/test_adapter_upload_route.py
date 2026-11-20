"""Member adapter uploads through the real Library: the route, the store, the ownership rule and the delete."""
from __future__ import annotations

import hashlib
import json
import os
import struct
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import proxy
from gateway import accounts, admin_store, adapter_upload, media_jobs, me_adapters, me_media, prompt_memory, receipt_store
from gateway.admin_oidc import AdminSession

from tests.test_adapter_upload import safetensors

ALPHA = safetensors(metadata={"ss_base_model_version": "alpha_base"})
UNKNOWN = safetensors({"u": [5]}, metadata={"ss_base_model_version": "no-such-base"})
FOLDER = "loras"


@pytest.fixture
def world(tmp_path, monkeypatch):
    admin, jobs, receipts = tmp_path / "data/admin.db", tmp_path / "data/media_jobs.db", tmp_path / "data/receipts.db"
    admin_store.init_db(admin)
    accounts.initialize(admin)
    media_jobs.init_db(jobs)
    receipt_store.init_db(receipts)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin)
    monkeypatch.setattr(media_jobs, "DB_PATH", jobs)
    monkeypatch.setattr(prompt_memory, "DB_PATH", jobs)
    monkeypatch.setattr(receipt_store, "DB_PATH", receipts)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "cache")
    engine = tmp_path / "engine"
    (engine / "models" / FOLDER).mkdir(parents=True)
    monkeypatch.setenv("AZIZAM_COMFY_DIR", str(engine))
    monkeypatch.setattr(adapter_upload, "architecture_table", lambda: {"alpha_base": {"ss_base_model_version": ("alpha_base",)}})
    people = {name: admin_store.upsert_oidc_user("iss", name, email=f"{name}@x.test", path=admin) for name in ("robin", "sam")}
    who = {"now": "robin"}

    def session(_request):
        if who["now"] is None:
            return None
        person = people[who["now"]]
        return AdminSession(subject=person["id"], email=person["email"], role="none",
                            issued_at=time.time(), expires_at=time.time() + 3600)

    app = FastAPI()
    app.include_router(me_adapters.create_me_adapters_router(
        session_provider=session, rules=proxy._adapter_rules, engine_local=proxy._adapter_engine_local,
        store=proxy._store_adapter_upload))
    app.include_router(me_media.create_me_media_router(
        session_provider=session,
        signed_url_builder=lambda request, asset_id, mode: f"http://testserver/v1/assets/{asset_id}/content?md={mode}",
        asset_path_resolver=proxy.asset_cache_path, asset_media_type_resolver=proxy.asset_media_type,
        asset_deleter=proxy._LIBRARY_ASSET_DELETER,
        asset_catalog_provider=me_media.create_asset_catalog_provider(proxy.IMAGE_CACHE_DIR),
        asset_metadata_reader=proxy._read_asset_metadata,
        admin_db_path=admin, media_jobs_path=jobs, receipts_path=receipts, prompt_memory_path=jobs))

    def turn_on(**setting):
        admin_store.put_operator_setting(adapter_upload.SETTING_KEY, {"enabled": True, **setting}, path=admin)

    return SimpleNamespace(client=TestClient(app), people=people, who=who, turn_on=turn_on, engine=engine / "models" / FOLDER,
                           cache=tmp_path / "cache", admin=admin)


def upload(world, data: bytes, name: str = "style.safetensors", *, content_type: str = "application/octet-stream"):
    return world.client.post("/me/api/adapters", files={"file": (name, data, content_type)})


def stored_files(world):
    return sorted(p.name for p in world.cache.iterdir()) if world.cache.exists() else []


def offers(world, person: str):
    return proxy.ADAPTER_LIBRARY.offers(world.people[person]["id"], folder=FOLDER)


def error_code(response) -> str:
    return response.json()["error"]["code"]


# --- refusals: nothing is stored ------------------------------------------------------------------------------------

def test_with_the_setting_off_an_upload_is_refused(world):
    response = upload(world, ALPHA)
    assert (response.status_code, error_code(response)) == (403, "adapter_uploads_off")
    assert stored_files(world) == []


def test_a_signed_out_caller_is_refused(world):
    world.turn_on()
    world.who["now"] = None
    assert upload(world, ALPHA).status_code == 401


@pytest.mark.parametrize("data,name", [
    (b"\x80\x04\x95" + b"\x00" * 200, "model.pt"),
    (b"PK\x03\x04" + b"\x00" * 200, "model.ckpt"),
    (b"\x00" * 300, "model.bin"),
    (b"just some words that are not a model", "style.safetensors"),
])
def test_a_file_that_is_not_safetensors_is_refused_whatever_its_name(world, data, name):
    world.turn_on()
    response = upload(world, data, name)
    assert (response.status_code, error_code(response)) == (400, "not_safetensors")
    assert stored_files(world) == []


def test_a_header_that_lies_about_its_offsets_is_refused(world):
    world.turn_on()

    def lie(header):
        header["layer.lora_up.weight"]["data_offsets"] = [8, 9_999_999]

    response = upload(world, safetensors(header_edit=lie))
    assert (response.status_code, error_code(response)) == (400, "adapter_header_invalid")
    assert stored_files(world) == []


def test_a_truncated_file_is_refused(world):
    world.turn_on()
    response = upload(world, ALPHA[:-4])
    assert (response.status_code, error_code(response)) == (400, "adapter_header_invalid")
    assert stored_files(world) == []


def test_a_file_over_the_cap_is_refused(world):
    world.turn_on(max_bytes=len(ALPHA) - 1)
    response = upload(world, ALPHA)
    assert (response.status_code, error_code(response)) == (413, "adapter_too_large")
    assert stored_files(world) == []


def test_when_the_engine_is_not_on_this_computer_the_upload_is_not_offered(world, monkeypatch):
    world.turn_on()
    monkeypatch.delenv("AZIZAM_COMFY_DIR")
    response = upload(world, ALPHA)
    assert (response.status_code, error_code(response)) == (409, "adapter_engine_not_local")
    assert stored_files(world) == []


# --- what is stored, and who sees it --------------------------------------------------------------------------------

def test_a_valid_upload_is_a_library_asset_of_kind_adapter_named_by_its_hash(world):
    world.turn_on()
    response = upload(world, ALPHA, "../../etc/evil name.safetensors")
    assert response.status_code == 200
    body = response.json()
    digest = hashlib.sha256(ALPHA).hexdigest()
    assert body["id"] == f"ast_{digest}" and body["kind"] == "adapter"
    assert (body["size_bytes"], body["offered"], body["architectures"]) == (len(ALPHA), True, ["alpha_base"])
    assert stored_files(world) == [f"{digest}.meta.json", f"{digest}.safetensors"]
    sidecar = json.loads((world.cache / f"{digest}.meta.json").read_text())
    assert sidecar["adapter"]["label"] == "evil name.safetensors"
    assert (world.cache / f"{digest}.safetensors").read_bytes() == ALPHA
    listed = world.client.get("/me/api/media?kind=adapter").json()["assets"]
    assert [(item["asset_id"], item["kind"]) for item in listed] == [(body["id"], "adapter")]


def test_an_upload_whose_base_is_unknown_is_stored_but_not_offered_and_the_response_says_so(world):
    world.turn_on()
    body = upload(world, UNKNOWN).json()
    assert body["offered"] is False and body["architectures"] == []
    assert "not offered" in body["note"]
    assert stored_files(world)
    [mine] = offers(world, "robin")
    assert mine.name == body["id"] and mine.architectures == ()
    assert adapter_upload.join([], offers(world, "robin"), "alpha_base") == []


def test_an_uploaded_adapter_is_offered_to_its_person_only(world):
    world.turn_on()
    asset_id = upload(world, ALPHA).json()["id"]
    assert adapter_upload.join(["op.safetensors"], offers(world, "robin"), "alpha_base") == ["op.safetensors", asset_id]
    assert offers(world, "sam") == []
    sam = world.people["sam"]["id"]
    assert proxy.ADAPTER_LIBRARY.resolve(sam, asset_id, folder=FOLDER) is None
    with pytest.raises(adapter_upload.UnknownAdapter):
        proxy.ADAPTER_LIBRARY.engine_names(sam, [asset_id], folder=FOLDER)
    with pytest.raises(adapter_upload.UnknownAdapter) as unknown:
        proxy.ADAPTER_LIBRARY.engine_names(sam, ["ast_" + "0" * 64], folder=FOLDER)
    assert "ast_" in str(unknown.value)       # the same exception for a name that exists and one that does not
    assert os.listdir(world.engine) == []


def test_turning_the_setting_off_again_leaves_only_operator_files_on_the_list(world):
    world.turn_on()
    asset_id = upload(world, ALPHA).json()["id"]
    admin_store.put_operator_setting(adapter_upload.SETTING_KEY, {"enabled": False}, path=world.admin)
    assert offers(world, "robin") == []
    assert proxy.ADAPTER_LIBRARY.resolve(world.people["robin"]["id"], asset_id, folder=FOLDER) is None


# --- the engine's copy ----------------------------------------------------------------------------------------------

def test_the_copy_in_the_engine_folder_is_named_by_the_hash_and_only_made_when_used(world):
    world.turn_on()
    asset_id = upload(world, ALPHA, "my private name.safetensors").json()["id"]
    assert os.listdir(world.engine) == []                         # an upload alone copies nothing
    robin = world.people["robin"]["id"]
    names = proxy.ADAPTER_LIBRARY.engine_names(robin, [asset_id], folder=FOLDER)
    digest = hashlib.sha256(ALPHA).hexdigest()
    assert names == {asset_id: f"{adapter_upload.SUBFOLDER}/{digest}.safetensors"}
    assert (world.engine / names[asset_id]).read_bytes() == ALPHA
    assert os.listdir(world.engine) == [adapter_upload.SUBFOLDER]
    assert json.loads((world.cache / f"{digest}.meta.json").read_text())["adapter"]["copies"] == [FOLDER]


# --- delete ---------------------------------------------------------------------------------------------------------

def test_deleting_the_asset_removes_it_from_the_list_and_removes_the_copy(world):
    world.turn_on()
    asset_id = upload(world, ALPHA).json()["id"]
    robin = world.people["robin"]["id"]
    [copy] = proxy.ADAPTER_LIBRARY.engine_names(robin, [asset_id], folder=FOLDER).values()
    assert (world.engine / copy).exists()
    deleted = world.client.delete(f"/me/api/media/{asset_id}")
    assert deleted.status_code == 200 and deleted.json()["physical_removed"] is True
    assert not (world.engine / copy).exists()
    assert stored_files(world) == []
    assert offers(world, "robin") == []
    assert proxy.ADAPTER_LIBRARY.resolve(robin, asset_id, folder=FOLDER) is None


def test_the_same_file_uploaded_again_after_a_delete_is_offered_again(world):
    world.turn_on()
    asset_id = upload(world, ALPHA).json()["id"]
    world.client.delete(f"/me/api/media/{asset_id}")
    assert upload(world, ALPHA).json()["id"] == asset_id
    assert [offer.name for offer in offers(world, "robin")] == [asset_id]


def test_two_people_with_the_same_file_each_keep_their_own_claim(world):
    world.turn_on()
    asset_id = upload(world, ALPHA).json()["id"]
    world.who["now"] = "sam"
    assert upload(world, ALPHA).json()["id"] == asset_id
    robin, sam = world.people["robin"]["id"], world.people["sam"]["id"]
    [copy] = proxy.ADAPTER_LIBRARY.engine_names(sam, [asset_id], folder=FOLDER).values()
    world.who["now"] = "robin"
    assert world.client.delete(f"/me/api/media/{asset_id}").json()["physical_removed"] is False
    assert offers(world, "robin") == []
    assert [offer.name for offer in offers(world, "sam")] == [asset_id]
    assert (world.engine / copy).exists()
    assert proxy.ADAPTER_LIBRARY.resolve(robin, asset_id, folder=FOLDER) is None
    assert proxy.ADAPTER_LIBRARY.resolve(sam, asset_id, folder=FOLDER) is not None
