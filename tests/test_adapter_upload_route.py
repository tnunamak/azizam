"""Member adapter uploads through the real Library: the route, the store, the ownership rule and the delete."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import tempfile
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import proxy
from gateway import accounts, admin_store, adapter_upload, media_jobs, me_adapters, me_media, prompt_memory, receipt_store
from gateway.admin_oidc import AdminSession
from gateway.request_body_limits import RequestBodyLimitMiddleware

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
    monkeypatch.setattr(proxy.ADAPTER_LIBRARY, "_materializers", {})
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
        store=proxy._store_adapter_upload, used_bytes=proxy.ADAPTER_LIBRARY.used_bytes))
    app.add_middleware(RequestBodyLimitMiddleware, limits=proxy._request_body_limits())
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

def test_a_valid_upload_is_a_library_asset_of_kind_adapter_with_a_personal_id(world):
    world.turn_on()
    response = upload(world, ALPHA, "../../etc/evil name.safetensors")
    assert response.status_code == 200
    body = response.json()
    content = hashlib.sha256(ALPHA).hexdigest()
    digest = adapter_upload.asset_digest(world.people["robin"]["id"], content)
    assert body["id"] == f"ast_{digest}" and body["kind"] == "adapter"
    assert (body["size_bytes"], body["offered"], body["architectures"]) == (len(ALPHA), True, ["alpha_base"])
    assert stored_files(world) == sorted([f"{digest}.meta.json", f"{digest}.safetensors"])
    sidecar = json.loads((world.cache / f"{digest}.meta.json").read_text())
    assert sidecar["adapter"] == {"label": "evil name.safetensors", "content_sha256": content}
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
    content = hashlib.sha256(ALPHA).hexdigest()
    assert names == {asset_id: f"{adapter_upload.SUBFOLDER}/{content}.safetensors"}
    assert (world.engine / names[asset_id]).read_bytes() == ALPHA
    assert os.listdir(world.engine) == [adapter_upload.SUBFOLDER]
    sidecar = json.loads((world.cache / f"{asset_id.split('_', 1)[1]}.meta.json").read_text())
    assert sidecar["adapter"]["copies"] == [FOLDER]


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


def test_two_people_with_the_same_file_each_have_their_own_asset_and_one_cannot_hide_the_others(world):
    world.turn_on()
    robins = upload(world, ALPHA).json()["id"]
    world.who["now"] = "sam"
    sams = upload(world, ALPHA).json()["id"]
    assert robins != sams
    robin, sam = world.people["robin"]["id"], world.people["sam"]["id"]
    [copy] = proxy.ADAPTER_LIBRARY.engine_names(sam, [sams], folder=FOLDER).values()
    assert proxy.ADAPTER_LIBRARY.engine_names(world.people["robin"]["id"], [robins], folder=FOLDER) == {robins: copy}
    world.who["now"] = "robin"
    assert world.client.delete(f"/me/api/media/{robins}").json()["physical_removed"] is True
    assert offers(world, "robin") == []
    assert [offer.name for offer in offers(world, "sam")] == [sams]
    assert (world.engine / copy).exists()                         # sam's upload is the same content, so it keeps the copy
    assert proxy.ADAPTER_LIBRARY.resolve(robin, sams, folder=FOLDER) is None
    assert proxy.ADAPTER_LIBRARY.resolve(sam, sams, folder=FOLDER) is not None
    world.who["now"] = "sam"
    world.client.delete(f"/me/api/media/{sams}")
    assert not (world.engine / copy).exists()


# --- review fixes: the body bound, the spool, the person's total, and what is not an adapter -----------------------------

MIB = 1 << 20


def big_adapter(mib: int = 2) -> bytes:
    return safetensors({"w": [mib * 512, 1024]}, metadata={"ss_base_model_version": "alpha_base"})


def test_the_gateways_body_limit_for_the_route_is_the_owners_cap_plus_one_mib(world):
    limits = proxy._request_body_limits()
    world.turn_on(max_bytes=5 * MIB)
    assert limits.for_path("/me/api/adapters") == 6 * MIB
    world.turn_on(max_bytes=MIB)
    assert limits.for_path("/me/api/adapters") == 2 * MIB
    assert any(m.cls is RequestBodyLimitMiddleware for m in proxy.app.user_middleware)


def test_a_declared_length_past_the_cap_is_refused_with_413_and_nothing_is_stored(world):
    world.turn_on(max_bytes=1000)
    response = world.client.post("/me/api/adapters", content=b"x" * (1000 + MIB + 10),
                                 headers={"content-type": "multipart/form-data; boundary=x"})
    assert response.status_code == 413 and error_code(response) == "request_too_large"
    assert stored_files(world) == []


def test_a_chunked_body_is_stopped_once_it_passes_the_bound_whatever_it_declares(world):
    world.turn_on(max_bytes=1000)
    chunks = (b"x" * MIB for _ in range(3))                     # no Content-Length: only the bytes counted can stop it
    response = world.client.post("/me/api/adapters", content=chunks,
                                 headers={"content-type": "multipart/form-data; boundary=x"})
    assert response.status_code == 413 and error_code(response) == "request_too_large"
    assert stored_files(world) == []


def test_a_body_past_the_spool_size_is_held_on_disk_under_the_temporary_directory_in_use(world, monkeypatch, tmp_path):
    spool = tmp_path / "private-tmp"
    spool.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool))        # what ``private_tmp.activate`` sets for the process
    seen = []
    real = adapter_upload.validate_stream

    def watching(stream, size, rules):
        backing = getattr(getattr(stream, "_file", None), "name", None)       # an open file: a path, or on Linux its descriptor
        seen.append(os.readlink(f"/proc/self/fd/{backing}") if isinstance(backing, int) else backing)
        return real(stream, size, rules)

    monkeypatch.setattr(adapter_upload, "validate_stream", watching)
    world.turn_on()
    assert upload(world, big_adapter()).status_code == 200
    assert seen and isinstance(seen[0], str) and seen[0].startswith(str(spool) + os.sep)


def test_a_person_cannot_store_more_than_the_owners_total_and_is_told_how_much_is_used(world):
    beta = safetensors({"b": [3]}, metadata={"ss_base_model_version": "alpha_base"})
    world.turn_on(max_total_bytes=len(ALPHA) + len(beta) - 1)
    assert upload(world, ALPHA).status_code == 200
    refused = upload(world, beta)
    assert (refused.status_code, error_code(refused)) == (413, "adapter_total_too_large")
    message = refused.json()["error"]["message"]
    assert str(len(ALPHA)) in message and str(len(ALPHA) + len(beta) - 1) in message
    assert len(stored_files(world)) == 2                       # the first file and its sidecar only
    world.who["now"] = "sam"
    assert upload(world, beta).status_code == 200              # the total is each person's own


def test_storing_the_same_file_again_does_not_count_twice_and_a_delete_frees_the_room(world):
    beta = safetensors({"b": [3]}, metadata={"ss_base_model_version": "alpha_base"})
    world.turn_on(max_total_bytes=len(ALPHA) + len(beta) - 1)
    first = upload(world, ALPHA).json()["id"]
    assert upload(world, ALPHA).json()["id"] == first
    assert upload(world, beta).status_code == 413
    world.client.delete(f"/me/api/media/{first}")
    assert upload(world, beta).status_code == 200


def test_a_graph_service_that_cannot_be_read_leaves_the_engine_unproven_local(world, monkeypatch):
    def broken(_capability):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(proxy, "_graph_service", broken)
    assert proxy._adapter_engine_local() is False
    world.turn_on()
    response = upload(world, ALPHA)
    assert (response.status_code, error_code(response)) == (409, "adapter_engine_not_local")


def test_noting_a_copy_for_an_asset_deleted_meanwhile_does_not_bring_its_sidecar_back(world):
    world.turn_on()
    asset_id = upload(world, ALPHA).json()["id"]
    world.client.delete(f"/me/api/media/{asset_id}")
    with pytest.raises(LookupError):
        proxy._note_adapter_copy(asset_id, FOLDER)
    assert stored_files(world) == []


def test_a_safetensors_file_stored_by_the_generic_path_is_not_an_adapter_and_is_never_offered(world):
    world.turn_on()
    robin = world.people["robin"]["id"]
    asset_id = proxy.store_asset_bytes(ALPHA, kind="ast", suffix=".safetensors", maker_person_id=robin)
    assert asset_id.startswith("ast_") and adapter_upload.digest_of(asset_id) is not None
    assert offers(world, "robin") == []
    assert proxy.ADAPTER_LIBRARY.resolve(robin, asset_id, folder=FOLDER) is None
    with pytest.raises(adapter_upload.UnknownAdapter):
        proxy.ADAPTER_LIBRARY.engine_names(robin, [asset_id], folder=FOLDER)
    assert os.listdir(world.engine) == []
    assert proxy.ADAPTER_LIBRARY.used_bytes(robin) == 0


def test_the_gateway_sweeps_its_adapter_copies_on_a_timer_and_the_lifespan_starts_that_timer(monkeypatch):
    calls = []
    monkeypatch.setattr(proxy.ADAPTER_LIBRARY, "sweep_all", lambda: calls.append(1) or [])
    monkeypatch.setattr(adapter_upload, "SWEEP_EVERY_S", 0.01)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(proxy._adapter_sweep_loop(stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(go())
    assert len(calls) >= 2
    assert "_adapter_sweep_loop" in inspect.getsource(proxy._serve)
