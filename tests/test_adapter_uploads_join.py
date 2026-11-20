"""A person's own uploaded adapters on the pick-lists: the run route, the image route, the member listing's source.

The engine is faked as in ``test_adapter_requests``: it lists operator files and answers their headers. An upload is real:
it goes through the gateway's own store, so the Library's ownership rule decides who sees it.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import proxy
from gateway import adapter_choices, adapter_upload, admin_store, media_jobs
from gateway.receipts import RouteReceipt
from gateway.runtime_auth import hash_api_key
from tests.support import gateway as support
from tests.test_adapter_requests import (  # noqa: F401  (engine_files is an autouse fixture)
    EXT, FILES, HEADERS, SERVICE, WORKFLOW, engine_files, run, serve)
from tests.test_adapter_upload import safetensors
from tests.media_test_bytes import png_bytes
from tests.test_workflow_run_contracts import (  # noqa: F401  (world is a fixture)
    FakeGraphEngine, call, manifest_of, serve_engine, template_of, world)

KREA = safetensors(metadata={"ss_base_model_version": "krea2"})
OTHER_BASE = safetensors({"u": [5]}, metadata={"ss_base_model_version": "no-such-base"})
STRANGER = "person-who-is-not-the-caller"
DECL = adapter_choices.declaration(manifest_of(WORKFLOW), "comfyui")
COPIES = adapter_upload.SUBFOLDER


@pytest.fixture
def uploads(world, monkeypatch, tmp_path):
    """The gateway as one person with a personal key sees it, with uploads on and an engine folder on this computer."""
    db = world.db
    engine = tmp_path / "engine"
    folder = engine / "models" / DECL.folder
    folder.mkdir(parents=True)
    monkeypatch.setenv("AZIZAM_COMFY_DIR", str(engine))
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(proxy.ADAPTER_LIBRARY, "_materializers", {})        # holds of an earlier test's folder are not this one's
    admin_store.put_operator_setting(adapter_upload.SETTING_KEY, {"enabled": True}, path=db)
    person = admin_store.upsert_oidc_user("https://fixture.test", "fixture-person", email="fixture@example.test", path=db)
    app = admin_store.create_app("Personal app", kind="personal", owner_user_id=person["id"], access_plan="person", path=db)
    key = f"opk_join_{tmp_path.name}"
    admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], app_id=app["id"], hash=hash_api_key(key),
                                       prefix=key[:16], created_by=person["id"], path=db)
    monkeypatch.setitem(support._APP_KEY, "key", key)       # calls are now made as the person, not as a shared App
    state = SimpleNamespace(person=person["id"], folder=folder, db=db)

    def add(data: bytes, person_id: str | None = None, label: str = "style.safetensors") -> str:
        checked = adapter_upload.validate_stream(io.BytesIO(data), len(data), proxy._adapter_rules())
        return proxy._store_adapter_upload(io.BytesIO(data), checked, person_id or person["id"], label)

    state.add = add
    return state


def lora_names(engine) -> list[str]:
    graph = engine.prompts[0]["prompt"]
    return [n["inputs"]["lora_name"] for n in graph.values() if isinstance(n, dict) and n.get("class_type") == "LoraLoaderModelOnly"]


def asset_of(data: bytes) -> str:
    return f"ast_{hashlib.sha256(data).hexdigest()}"


# --- the run route --------------------------------------------------------------------------------------------------

def test_a_run_may_name_the_persons_own_upload_and_the_engine_gets_the_copy_named_by_its_hash(world, uploads):
    asset_id = uploads.add(KREA)
    engine = serve(world)
    response = run(WORKFLOW, {"prompt": "a red fox", "loras": [{"name": asset_id, "weight": 0.5}]})
    assert response.status_code == 200 and response.json()["status"] == "succeeded", response.text
    engine_name = f"{COPIES}/{hashlib.sha256(KREA).hexdigest()}.safetensors"
    assert lora_names(engine) == [engine_name]
    assert (uploads.folder / engine_name).read_bytes() == KREA
    job = media_jobs.get(response.json()["job_id"], path=world.media)
    assert job["metadata"]["adapters"] == {"loras": [{"name": engine_name, "weight": 0.5}]}


def test_an_operator_file_and_an_upload_can_be_used_together(world, uploads):
    asset_id = uploads.add(KREA)
    engine = serve(world)
    response = run(WORKFLOW, {"prompt": "x", "loras": [{"name": "example_style_v1.safetensors"}, {"name": asset_id}]})
    assert response.status_code == 200, response.text
    assert lora_names(engine)[0] == "example_style_v1.safetensors" and lora_names(engine)[1].startswith(COPIES + "/")


def test_another_persons_upload_is_refused_exactly_as_a_name_that_does_not_exist(world, uploads):
    theirs = uploads.add(KREA, STRANGER)
    engine = serve(world)
    refused = run(WORKFLOW, {"prompt": "x", "loras": [{"name": theirs}]})
    never = run(WORKFLOW, {"prompt": "x", "loras": [{"name": "ast_" + "0" * 64}]})
    assert refused.status_code == never.status_code == 422
    assert refused.json()["error"]["problems"] == never.json()["error"]["problems"]
    assert engine.calls == [] and list(uploads.folder.iterdir()) == []       # nothing was copied for them either


def test_an_upload_whose_base_is_unknown_is_not_offered_to_a_run(world, uploads):
    asset_id = uploads.add(OTHER_BASE)
    serve(world)
    response = run(WORKFLOW, {"prompt": "x", "loras": [{"name": asset_id}]})
    assert response.status_code == 422 and list(uploads.folder.iterdir()) == []


def test_with_the_setting_off_an_upload_is_not_offered_to_a_run(world, uploads):
    asset_id = uploads.add(KREA)
    admin_store.put_operator_setting(adapter_upload.SETTING_KEY, {"enabled": False}, path=uploads.db)
    serve(world)
    assert run(WORKFLOW, {"prompt": "x", "loras": [{"name": asset_id}]}).status_code == 422
    assert list(uploads.folder.iterdir()) == []


def test_a_key_that_is_not_a_persons_is_offered_no_uploads(world, uploads, monkeypatch):
    asset_id = uploads.add(KREA)
    serve(world)
    monkeypatch.setattr(proxy, "_auth_person_id", lambda _auth: None)
    assert run(WORKFLOW, {"prompt": "x", "loras": [{"name": asset_id}]}).status_code == 422


def test_the_gateways_own_copy_cannot_be_named_directly_by_anyone(world, uploads, engine_files, monkeypatch):
    """The engine lists the copies it holds. A copy is one person's file: it is never the operator's, whatever its header."""
    asset_id = uploads.add(KREA, STRANGER)
    copy_name = f"{COPIES}/{hashlib.sha256(KREA).hexdigest()}.safetensors"
    monkeypatch.setitem(HEADERS, copy_name, {"ss_base_model_version": "krea2"})
    monkeypatch.setattr(adapter_choices, "default_list_files", lambda *_: [*FILES, copy_name])
    serve(world)
    response = run(WORKFLOW, {"prompt": "x", "loras": [{"name": copy_name}]})
    assert response.status_code == 422, response.text
    assert copy_name not in response.json()["error"]["problems"][0]["message"] and asset_id not in response.text


def test_a_copy_that_cannot_be_made_is_a_retryable_refusal(world, uploads, monkeypatch):
    asset_id = uploads.add(KREA)
    engine = serve(world)

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(proxy.ADAPTER_LIBRARY, "engine_names", fail)
    response = run(WORKFLOW, {"prompt": "x", "loras": [{"name": asset_id}]})
    assert response.status_code == 503 and response.json()["error"]["code"] == "adapter_upload_unavailable"
    assert engine.calls == []


def test_a_run_that_names_no_upload_copies_nothing(world, uploads):
    uploads.add(KREA)
    serve(world)
    assert run(WORKFLOW, {"prompt": "x", "loras": [{"name": "example_style_v1.safetensors"}]}).status_code == 200
    assert list(uploads.folder.iterdir()) == []


# --- the image route ------------------------------------------------------------------------------------------------

def image_call(uploads, block, *, person: str | None):
    manifest = manifest_of(WORKFLOW)
    receipt = RouteReceipt(protocol="openai", intent="image.generate", path="/v1/images/generations", request_id="req-join")
    auth = proxy.AuthContext(identity=SimpleNamespace(person_id=person)) if person else None
    with patch.object(proxy, "_graph_service", lambda _capability: SERVICE):
        graph, response = asyncio.run(proxy._apply_workflow_extensions_to_image_request(
            request_body={"prompt": "x", "extensions": {EXT: block}}, manifest=manifest,
            backend_request=copy.deepcopy(template_of(manifest)), standard_fields=proxy.STANDARD_IMAGE_GENERATE_FIELDS,
            receipt=receipt, auth_context=auth))
    return graph, response, receipt


def test_the_image_route_binds_the_persons_upload_as_the_engines_copy(uploads):
    asset_id = uploads.add(KREA)
    graph, response, receipt = image_call(uploads, {"loras": [{"name": asset_id, "weight": 0.25}]}, person=uploads.person)
    assert response is None
    names = [n["inputs"]["lora_name"] for n in graph.values() if isinstance(n, dict) and n.get("class_type") == "LoraLoaderModelOnly"]
    engine_name = f"{COPIES}/{hashlib.sha256(KREA).hexdigest()}.safetensors"
    assert names == [engine_name] and (uploads.folder / engine_name).read_bytes() == KREA
    assert receipt.metadata["adapters"] == {"loras": [{"name": engine_name, "weight": 0.25}]}


def test_the_image_route_refuses_another_persons_upload_like_an_unknown_name(uploads):
    theirs = uploads.add(KREA, STRANGER)
    _, refused, _ = image_call(uploads, {"loras": [{"name": theirs}]}, person=uploads.person)
    _, never, _ = image_call(uploads, {"loras": [{"name": "ast_" + "0" * 64}]}, person=uploads.person)
    assert refused.status_code == never.status_code == 422
    assert json.loads(refused.body)["error"]["message"] == json.loads(never.body)["error"]["message"].replace("0" * 64, theirs[4:])
    assert list(uploads.folder.iterdir()) == []


def test_the_image_route_offers_no_upload_to_a_caller_who_is_not_a_person(uploads):
    asset_id = uploads.add(KREA)
    _, response, _ = image_call(uploads, {"loras": [{"name": asset_id}]}, person=None)
    assert response is not None and response.status_code == 422


def test_the_image_route_says_so_when_a_copy_cannot_be_made(uploads, monkeypatch):
    asset_id = uploads.add(KREA)
    monkeypatch.setattr(proxy.ADAPTER_LIBRARY, "engine_names", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    _, response, _ = image_call(uploads, {"loras": [{"name": asset_id}]}, person=uploads.person)
    assert response.status_code == 503 and json.loads(response.body)["error"]["code"] == "adapter_upload_unavailable"


# --- the pick-list --------------------------------------------------------------------------------------------------

def choices_for(person: str | None):
    return asyncio.run(adapter_choices.installed(DECL, service_id="comfyui", origin="http://engine.invalid:7890", person_id=person))


def test_the_pick_list_for_a_person_is_the_operators_files_then_their_own_uploads(uploads):
    mine, theirs = uploads.add(KREA), uploads.add(safetensors({"z": [9]}, metadata={"ss_base_model_version": "krea2"}), STRANGER)
    mine_list = choices_for(uploads.person)
    assert mine_list.loras == ("example_style_v1.safetensors", mine) and mine_list.uploads == (mine,)
    assert choices_for(STRANGER).loras == ("example_style_v1.safetensors", theirs)
    assert choices_for(None).loras == ("example_style_v1.safetensors",)


def test_an_upload_never_enters_the_operators_cached_list(uploads):
    uploads.add(KREA)
    choices_for(uploads.person)
    assert choices_for("someone-else").loras == ("example_style_v1.safetensors",)


def test_the_pick_list_has_no_upload_that_is_unknown_or_off(uploads):
    uploads.add(OTHER_BASE)
    assert choices_for(uploads.person).uploads == ()
    uploads.add(KREA)
    admin_store.put_operator_setting(adapter_upload.SETTING_KEY, {"enabled": False}, path=uploads.db)
    assert choices_for(uploads.person).uploads == ()


def test_the_listing_offers_the_same_names_to_the_person_only(uploads, monkeypatch):
    asset_id = uploads.add(KREA)
    record = SimpleNamespace(id=WORKFLOW, contract=manifest_of(WORKFLOW))

    def listed(person):
        return asyncio.run(adapter_choices.offered([record], "comfyui", service_id="comfyui",
                                                   origin="http://engine.invalid:7890", person_id=person,
                                                   list_files=adapter_choices.default_list_files,
                                                   read_header=adapter_choices.default_read_header))

    assert listed(uploads.person)[WORKFLOW] == ("example_style_v1.safetensors", asset_id)
    assert listed(STRANGER)[WORKFLOW] == ("example_style_v1.safetensors",)


def test_a_deleted_upload_leaves_the_pick_list_and_the_engines_folder(world, uploads):
    asset_id = uploads.add(KREA)
    serve(world)
    assert run(WORKFLOW, {"prompt": "x", "loras": [{"name": asset_id}]}).status_code == 200
    [copy_path] = list((uploads.folder / COPIES).iterdir())
    proxy._LIBRARY_ASSET_DELETER(asset_id, release_person_id=uploads.person)
    assert not copy_path.exists() and choices_for(uploads.person).uploads == ()


# --- review fixes: a name that is not text, an upload that vanished, and a copy held for the life of its run -----------

def test_an_adapter_whose_name_is_not_text_is_a_422_beside_a_real_upload_not_a_crash(world, uploads):
    asset_id = uploads.add(KREA)
    engine = serve(world)
    response = run(WORKFLOW, {"prompt": "x", "loras": [{"name": asset_id}, {"name": ["a", "b"]}]})
    assert response.status_code == 422, response.text
    assert engine.calls == []


def test_the_image_route_answers_a_name_that_is_not_text_with_a_422(uploads):
    asset_id = uploads.add(KREA)
    _, response, _ = image_call(uploads, {"loras": [{"name": asset_id}, {"name": {"x": 1}}]}, person=uploads.person)
    assert response is not None and response.status_code == 422


def test_an_upload_deleted_between_the_list_and_the_copy_is_refused_as_any_unknown_name(world, uploads, monkeypatch):
    asset_id = uploads.add(KREA)
    engine = serve(world)

    def vanished(*_args, **_kwargs):
        raise adapter_upload.UnknownAdapter(asset_id)

    monkeypatch.setattr(proxy.ADAPTER_LIBRARY, "engine_names", vanished)
    response = run(WORKFLOW, {"prompt": "x", "loras": [{"name": asset_id}]})
    assert response.status_code == 422, response.text
    assert engine.calls == []


class WatchingEngine(FakeGraphEngine):
    """Notes which copies the gateway held at the moment the engine was given the graph."""

    held_at_submit: list[set[str]] = []

    def __call__(self, request):
        if request.method == "POST" and request.url.path == "/prompt":
            self.held_at_submit.append(set(proxy.ADAPTER_LIBRARY.held_digests()))
        return super().__call__(request)


def watching(world):
    WatchingEngine.held_at_submit = []
    return serve_engine(world, WatchingEngine(manifest_of(WORKFLOW), png_bytes((9, 9, 9)), "image/png", "out.png"))


def test_a_run_holds_its_copy_while_the_engine_has_it_and_lets_go_when_the_run_ends(world, uploads):
    asset_id = uploads.add(KREA)
    watching(world)
    response = run(WORKFLOW, {"prompt": "x", "loras": [{"name": asset_id}]})
    assert response.status_code == 200 and response.json()["status"] == "succeeded", response.text
    assert WatchingEngine.held_at_submit == [{hashlib.sha256(KREA).hexdigest()}]
    assert proxy.ADAPTER_LIBRARY.held_digests() == set()


def test_a_queued_job_keeps_its_copy_until_it_has_run_whatever_a_sweep_or_a_delete_does(world, uploads, monkeypatch):
    asset_id = uploads.add(KREA)
    watching(world)
    monkeypatch.setattr(proxy._WORKFLOW_RUN_PORT, "schedule", lambda job_id: None)     # queued, not yet run
    queued = call("post", f"/gateway/v1/workflows/{WORKFLOW}/run", json={"inputs": {"prompt": "x", "loras": [{"name": asset_id}]}})
    assert queued.status_code == 202, queued.text
    digest = hashlib.sha256(KREA).hexdigest()
    copy_path = uploads.folder / COPIES / f"{digest}.safetensors"
    os.utime(copy_path, (1, 1))
    proxy.ADAPTER_LIBRARY.sweep(folder=DECL.folder)
    proxy._LIBRARY_ASSET_DELETER(asset_id, release_person_id=uploads.person)
    assert copy_path.exists()                                     # held for the queued job, so neither removed it
    asyncio.run(proxy._run_workflow_job(queued.json()["job_id"]))
    assert media_jobs.get(queued.json()["job_id"], path=world.media)["status"] == "succeeded"
    assert not copy_path.exists()                                 # the delete took effect when the job let go
    assert proxy.ADAPTER_LIBRARY.held_digests() == set()


def test_a_run_refused_after_its_copy_was_made_does_not_keep_the_copy_held(world, uploads):
    asset_id = uploads.add(KREA)
    serve(world)
    response = run(WORKFLOW, {"prompt": "x", "loras": [{"name": asset_id, "weight": 99}]})
    assert response.status_code == 422, response.text
    assert proxy.ADAPTER_LIBRARY.held_digests() == set()


def test_the_image_route_holds_its_copy_for_a_bounded_time(uploads):
    asset_id = uploads.add(KREA)
    image_call(uploads, {"loras": [{"name": asset_id}]}, person=uploads.person)
    assert proxy.ADAPTER_LIBRARY.held_digests() == {hashlib.sha256(KREA).hexdigest()}


def test_a_queued_job_that_is_canceled_lets_go_of_its_copy_and_a_waiting_delete_then_takes_effect(world, uploads, monkeypatch):
    asset_id = uploads.add(KREA)
    serve(world)
    monkeypatch.setattr(proxy._WORKFLOW_RUN_PORT, "schedule", lambda job_id: None)
    queued = call("post", f"/gateway/v1/workflows/{WORKFLOW}/run", json={"inputs": {"prompt": "x", "loras": [{"name": asset_id}]}})
    assert queued.status_code == 202, queued.text
    copy_path = uploads.folder / COPIES / f"{hashlib.sha256(KREA).hexdigest()}.safetensors"
    proxy._LIBRARY_ASSET_DELETER(asset_id, release_person_id=uploads.person)
    assert copy_path.exists() and proxy.ADAPTER_LIBRARY.held_digests()
    job_id = queued.json()["job_id"]
    monkeypatch.setattr(proxy._WORKFLOW_RUN_PORT, "can_cancel", lambda job, caller: True)     # the caller's rights are not what is tried here
    service = proxy._workflow_job_service()
    monkeypatch.setattr(service, "_readable", lambda jid, caller: media_jobs.get(jid, audience="internal", path=world.media))
    assert service.cancel(job_id, None).status == "canceled"
    assert proxy.ADAPTER_LIBRARY.held_digests() == set() and not copy_path.exists()
