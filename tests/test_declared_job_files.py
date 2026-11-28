"""A declared media job binds Library assets to the named file inputs its operation declares.

The route under test is ``POST /gateway/v1/media/jobs`` with ``"files": {"<name>": {"asset_id": "<id>"}}``. The names and
the media types they take come from the operation's description (``tests/fixtures/engine_protocol/d0/smell-file.yml``);
no product code names an operation, engine or input. Each refusal happens at submit: the answer is a 4xx with a stable
code, no job exists afterwards and the engine was never called. Nothing here patches a product function.
"""
from __future__ import annotations

import pytest

from tests.support.declared_file_engine import (
    PNG, VECTOR, WAV, app_key, headers, make_asset, refine, start_engine, submit, wait,
)
from tests.support.fake_upstream import fake_upstreams  # noqa: F401
from tests.test_owner_engine_chain_smell import SmellEngine, owner, smell_engine  # noqa: F401
from tests.test_signin_who_can_enter import csrf_for, gateway  # noqa: F401


@pytest.fixture
def rig(owner, gateway, fake_upstreams):  # noqa: F811 - fixtures
    client, _, owner_id, plan_id = owner
    engine = start_engine(client, fake_upstreams)
    return client, owner_id, plan_id, engine, gateway, app_key(client, owner_id, plan_id)


def _job_count(public, key) -> int:
    listed = public.get("/gateway/v1/media/jobs", headers=headers(key))
    assert listed.status_code == 200, listed.text
    return len(listed.json()["jobs"])


def _refused(public, key, engine, body, status, code, param=None):
    """The submit is refused before anything runs: the code is stable, no job was made and the engine heard nothing."""
    before = _job_count(public, key)
    response = submit(public, key, body)
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code, error
    if param is not None:
        assert error["param"] == param, error
    assert _job_count(public, key) == before
    assert engine.refines == []
    return response


def test_two_bound_assets_reach_the_engine_as_named_parts_with_their_bytes_and_types(rig):
    _, _, _, engine, gateway, key = rig
    with gateway.client() as public:
        png_id = make_asset(public, key)
        engine.result = (WAV, "audio/wav")
        wav_id = make_asset(public, key)
        response = submit(public, key, refine(image=png_id, reference=wav_id))
        assert response.status_code == 202, response.text
        assert wait(public, key, response.json())["status"] == "succeeded"
    (received,) = engine.refines
    assert received["image"]["bytes"] == PNG and received["image"]["content_type"] == "image/png"
    assert received["reference"]["bytes"] == WAV and received["reference"]["content_type"] == "audio/wav"
    assert received["vector"]["bytes"] == b"[0.25,-1.5,3.0,0.0]"


def test_an_unknown_asset_is_refused_at_submit(rig):
    _, _, _, engine, gateway, key = rig
    with gateway.client() as public:
        _refused(public, key, engine, refine(image="img_" + "0" * 64), 400, "file_asset_not_found", "files.image")


def test_an_asset_id_that_is_not_an_asset_id_is_refused_like_an_unknown_one(rig):
    _, _, _, engine, gateway, key = rig
    with gateway.client() as public:
        _refused(public, key, engine, refine(image="../../etc/passwd"), 400, "file_asset_not_found", "files.image")


def test_another_apps_asset_is_refused_exactly_like_an_unknown_one(rig):
    client, owner_id, plan_id, engine, gateway, key = rig
    other = app_key(client, owner_id, plan_id, name="other-app")
    with gateway.client() as public:
        theirs = make_asset(public, other)
        unknown = _refused(public, key, engine, refine(image="img_" + "0" * 64), 400, "file_asset_not_found", "files.image")
        refused = _refused(public, key, engine, refine(image=theirs), 400, "file_asset_not_found", "files.image")
        # The answer cannot tell the caller that the asset exists: the bodies are the same bytes.
        assert refused.content == unknown.content
        assert theirs not in refused.text
        # The maker still has it: the asset was not touched.
        owned = submit(public, other, refine(image=theirs))
        assert owned.status_code == 202, owned.text
        assert wait(public, other, owned.json())["status"] == "succeeded"


def test_another_persons_asset_is_refused_exactly_like_an_unknown_one(rig):
    import proxy
    _, owner_id, _, engine, gateway, key = rig
    # A file the owner made as a person (the Library holds it under the person, not under any app). Seeded through the
    # store's own write; the key belongs to an app, which owns nothing a person made.
    theirs = proxy.store_asset_bytes(PNG, kind="img", suffix=".png", owner_key_hash="user:" + owner_id)
    with gateway.client() as public:
        unknown = _refused(public, key, engine, refine(image="img_" + "0" * 64), 400, "file_asset_not_found", "files.image")
        refused = _refused(public, key, engine, refine(image=theirs), 400, "file_asset_not_found", "files.image")
        assert refused.content == unknown.content
        assert theirs not in refused.text


def test_a_file_name_the_operation_does_not_declare_is_refused(rig):
    _, _, _, engine, gateway, key = rig
    with gateway.client() as public:
        asset = make_asset(public, key)
        response = _refused(public, key, engine, refine(image=asset, mask=asset), 400, "unknown_file_input", "files.mask")
        assert "image" in response.json()["error"]["message"] and "reference" in response.json()["error"]["message"]


def test_an_asset_whose_type_the_input_does_not_take_is_refused(rig):
    _, _, _, engine, gateway, key = rig
    with gateway.client() as public:
        png_id = make_asset(public, key)
        engine.result = (WAV, "audio/wav")
        wav_id = make_asset(public, key)
        # `image` takes pictures, `reference` takes audio: the declaration decides, whatever the asset's name says.
        _refused(public, key, engine, refine(image=wav_id), 400, "file_type_not_accepted", "files.image")
        _refused(public, key, engine, refine(image=png_id, reference=png_id), 400, "file_type_not_accepted", "files.reference")


def test_files_sent_to_an_operation_with_no_file_inputs_are_refused(rig):
    _, _, _, engine, gateway, key = rig
    with gateway.client() as public:
        asset = make_asset(public, key)
        _refused(public, key, engine, {"intent": "smell.infer", "model": "smell-1", "input": {"vector": VECTOR},
                                       "files": {"image": {"asset_id": asset}}}, 400, "files_not_accepted", "files")


def test_files_sent_to_a_job_that_reads_its_inputs_from_the_request_are_refused_not_dropped(rig):
    _, _, _, engine, gateway, key = rig
    with gateway.client() as public:
        asset = make_asset(public, key)
        _refused(public, key, engine, {"intent": "image.generate", "prompt": "a red fox",
                                       "files": {"image": {"asset_id": asset}}}, 400, "files_not_accepted", "files")


def test_a_required_file_input_that_is_missing_is_refused(rig):
    _, _, _, engine, gateway, key = rig
    with gateway.client() as public:
        _refused(public, key, engine, refine(), 400, "missing_file_input", "files.image")


@pytest.mark.parametrize("files", [["img_x"], {"image": "img_x"}, {"image": {"asset_id": "x", "url": "http://x"}},
                                   {"image": {"asset_id": 5}}])
def test_a_files_value_that_is_not_a_name_to_asset_map_is_refused(rig, files):
    _, _, _, engine, gateway, key = rig
    with gateway.client() as public:
        _refused(public, key, engine, {**refine(), "files": files}, 400, "invalid_files")


class _Resolver:
    """A stand-in for the media input resolver's two methods the binder calls: it returns what its table holds."""

    def __init__(self, assets):
        self.assets, self.released = assets, []

    def resolve_named_asset(self, name, asset_id, caller, *, holder, destination, declared_inputs):
        from gateway import media_inputs
        if name not in declared_inputs:
            raise media_inputs.MediaInputError(media_inputs.INVALID, "not declared")
        data, media_type = self.assets[asset_id]
        stored = media_inputs.StoredInput(asset_id, media_type, len(data), "0" * 64, "lease")
        return name, media_inputs.ResolvedInput(stored, data)

    def release(self, holder):
        self.released.append(holder)


def _bind(resolver, files, limit):
    from gateway import declared_job_files, media_inputs
    from gateway.engine_protocol.description import FileInput
    return declared_job_files.bind(
        files, {"a": FileInput(("image/*",)), "b": FileInput(("image/*",))}, resolver=resolver, caller=object(),
        holder=media_inputs.Holder("job", "job_1"), destination=media_inputs.Destination("svc"), max_upload_bytes=limit)


def test_the_upload_limit_counts_each_bound_part_and_refuses_before_a_job_exists():
    from gateway import declared_job_files
    resolver = _Resolver({"img_1": (PNG, "image/png")})
    both = {"a": {"asset_id": "img_1"}, "b": {"asset_id": "img_1"}}
    # One asset bound to two names is sent twice, so it counts twice: the same sum the executor's budget enforces.
    assert set(_bind(resolver, both, 2 * len(PNG)).refs) == {"a", "b"}
    with pytest.raises(declared_job_files.FileBindingError) as refused:
        _bind(resolver, both, 2 * len(PNG) - 1)
    assert (refused.value.status, refused.value.code) == (413, "files_too_large")
    # The leases the refused request took end at once; the holder is the job id that will never exist.
    assert [holder.id for holder in resolver.released] == ["job_1"]
