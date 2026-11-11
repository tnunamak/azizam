"""An image model name finds its workflow record by data (the manifest's binding file stem), not by spelling.

``/v1/models`` names a model by its graph file stem; the record id is the manifest id. Two shipped edit workflows
differ (``flux2-klein-4b-edit`` is ``image.edit.flux2-klein-4b``), so a job and a plan check must use the record's id.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import proxy
from gateway.workflow_identity import AmbiguousWorkflowStem, find_workflow_record
from tests.support.access import authenticated, identity, plan_for
from tests.support.s7_parity_images_rig import observe
from tests import test_adapter_differential as base
from tests.support.adapter_differential_rig import comfy_exchanges, history_done
from tests.test_s7_parity_images import EDIT, GEN, JOB, body, form, job_body, job_setup, media_jobs, ok

GEN_INTENT, EDIT_INTENT = "image.generate", "image.edit"

# (intent, name) -> record id, written by hand from the shipped manifests: every graph file stem and every short id.
TABLE = [
    (GEN_INTENT, "anima-turbo", "image.generate.anima-turbo"),
    (GEN_INTENT, "flux2-klein-4b", "image.generate.flux2-klein-4b"),
    (GEN_INTENT, "flux2-klein-4b-seamless-tile", "image.generate.flux2-klein-4b-seamless-tile"),
    (GEN_INTENT, "flux2-klein-snofs", "image.generate.flux2-klein-snofs"),
    (GEN_INTENT, "flux2-klein", "image.generate.flux2-klein"),
    (GEN_INTENT, "krea2-turbo-snofs", "image.generate.krea2-turbo-snofs"),
    (GEN_INTENT, "krea2-turbo", "image.generate.krea2-turbo"),
    (GEN_INTENT, "moss-image-fast", "image.generate.moss-image-fast"),
    (GEN_INTENT, "moss-image-quality", "image.generate.moss-image-quality"),
    (GEN_INTENT, "moss-image", "image.generate.moss-image"),
    (GEN_INTENT, "sdxl-seamless-tile", "image.generate.sdxl-seamless-tile"),
    (EDIT_INTENT, "fibo-2-edit-turbo", "image.edit.fibo-2-edit-turbo"),
    (EDIT_INTENT, "flux2-klein-4b-edit", "image.edit.flux2-klein-4b"),
    (EDIT_INTENT, "flux2-klein-4b", "image.edit.flux2-klein-4b"),
    (EDIT_INTENT, "flux2-klein-4b-inpaint", "image.edit.flux2-klein-4b-inpaint"),
    (EDIT_INTENT, "flux2-klein-edit", "image.edit.flux2-klein"),
    (EDIT_INTENT, "flux2-klein", "image.edit.flux2-klein"),
    (EDIT_INTENT, "qwen-image-2.1-identity", "image.edit.image-identity"),
    (EDIT_INTENT, "qwen-image-2.1-edit-multiref", "image.edit.qwen-image-2.1-edit-multiref"),
    (EDIT_INTENT, "qwen-image-2.1-edit", "image.edit.qwen-image-2.1-edit"),
    (EDIT_INTENT, "qwen-image-2.1-inpaint", "image.edit.qwen-image-2.1-inpaint"),
    (EDIT_INTENT, "seedvr2-upscale", "image.edit.seedvr2-upscale"),
    ("image.analyze", "face-likeness", "image.analyze.face-likeness"),
    # An edit workflow's stem names no generate workflow, and the reverse; the old guess matched the first two.
    (GEN_INTENT, "flux2-klein-4b-edit", None),
    (GEN_INTENT, "flux2-klein-edit", None),
    (GEN_INTENT, "qwen-image-2.1-identity", None),
    (EDIT_INTENT, "no-such-model", None),
]


@pytest.mark.parametrize(("intent", "name", "expected"), TABLE, ids=[f"{i}:{n}" for i, n, _ in TABLE])
def test_shipped_stem_or_short_id_finds_its_record(intent, name, expected):
    record = proxy._image_workflow_record(intent, name)
    assert (str(record.manifest["id"]) if record is not None else None) == expected


def _record(manifest_id, intent, stem=None):
    bindings = {"comfyui": {"workflow": f"workflows/images/{stem}.json"}} if stem else {}
    return SimpleNamespace(manifest={"id": manifest_id, "intent": intent, "bindings": bindings})


def test_exact_id_is_used_only_when_no_stem_matches():
    by_stem, by_id = _record("image.edit.a", EDIT_INTENT, "b"), _record("image.edit.b", EDIT_INTENT, "other")
    assert find_workflow_record([by_id, by_stem], EDIT_INTENT, "b") is by_stem
    assert find_workflow_record([by_id, by_stem], EDIT_INTENT, "nothing") is None


def test_a_record_of_another_intent_is_never_chosen():
    assert find_workflow_record([_record("image.edit.a", EDIT_INTENT, "a")], GEN_INTENT, "a") is None


@pytest.mark.parametrize("order", [0, 1])
def test_two_records_of_one_intent_sharing_a_stem_raise(order):
    records = [_record("image.edit.one", EDIT_INTENT, "shared"), _record("image.edit.two", EDIT_INTENT, "shared")]
    with pytest.raises(AmbiguousWorkflowStem, match="image.edit.one.*image.edit.two"):
        find_workflow_record(records[::1 - 2 * order], EDIT_INTENT, "shared")


def test_the_same_stem_under_two_intents_is_not_ambiguous():
    records = [_record("image.edit.x", EDIT_INTENT, "shared"), _record("image.generate.x", GEN_INTENT, "shared")]
    assert find_workflow_record(records, GEN_INTENT, "shared") is records[1]


def test_shipped_registry_has_no_shared_stem_within_an_intent():
    """The lookup raises on a shared stem, so the shipped records must not have one."""
    for intent, name, _ in TABLE:
        proxy._image_workflow_record(intent, name)


def test_identity_edit_is_found_by_its_binding_stem():
    """Case 3: the model is ``qwen-image-2.1-identity``; the record id is ``image.edit.image-identity``."""
    assert proxy._image_workflow_record(EDIT_INTENT, "qwen-image-2.1-identity").manifest["id"] == "image.edit.image-identity"


def test_job_workflow_is_the_record_id_and_keeps_the_guess_without_a_record():
    assert proxy._image_job_workflow_id(EDIT_INTENT, "flux2-klein-4b-edit") == "image.edit.flux2-klein-4b"
    assert proxy._image_job_workflow_id(GEN_INTENT, "flux2-klein-4b") == "image.generate.flux2-klein-4b"
    # An owner graph with no manifest keeps today's spelling.
    assert proxy._image_job_workflow_id(GEN_INTENT, "my-own-graph") == "image.generate.my-own-graph"
    assert proxy._SESSION_MAKE_RUNTIME.workflow_id(EDIT_INTENT, "flux2-klein-4b-edit") == "image.edit.flux2-klein-4b"


def _plan_setup(workflow):
    def setup(rig):
        # The flux2-klein-4b-edit graph publishes its image from node 9.
        base._comfy(rig, comfy_exchanges(views=[(base._png((20, 20, 20)), "image/png")], history=history_done(node="9"), strict=False))
        rig._exit.enter_context(authenticated(identity(plan_for(caps=("image",), workflows=[workflow]))))
    return setup


def _edit(tmp_path, workflow):
    return observe(tmp_path, EDIT, form(model="flux2-klein-4b-edit"), _plan_setup(workflow), row=False, name="plan")


def test_a_plan_listing_the_record_id_admits_the_edit_model(tmp_path):
    obs = _edit(tmp_path, "image.edit.flux2-klein-4b")
    assert obs.status == 200, obs.body


def test_a_plan_listing_only_the_old_fake_spelling_refuses_the_edit_model(tmp_path):
    obs = _edit(tmp_path, "image.edit.flux2-klein-4b-edit")
    assert obs.status == 403, obs.body
    assert obs.engine == []


@pytest.mark.parametrize("model", ["flux2-klein-4b-edit", "flux2-klein-edit"])
def test_generating_with_an_edit_model_is_a_clean_refusal(tmp_path, model):
    """Before the join this was a 500 with no engine call: the generate guess picked a record whose role map did not fit
    the edit graph. Now the model is not a generate workflow: a 404 ``model_not_found``, and the engine is never called."""
    obs = observe(tmp_path, GEN, body(model=model), ok, row=False, name="refuse")
    assert obs.status == 404, obs.body
    assert '"code": "model_not_found"' in obs.body["text"]
    assert "not available for image generation" in obs.body["text"]
    assert not [e for e in obs.engine if e[1] == "/prompt"]


@pytest.mark.parametrize("model", ["flux2-klein-4b-edit", "flux2-klein-edit"])
def test_an_image_job_naming_an_edit_model_is_refused_at_submit(tmp_path, model):
    """The async job route used to accept an edit model, store a made-up generate workflow and fail later when the job
    ran. It now refuses at submit like the sync route: 404 ``model_not_found``, no job row, no engine call."""
    obs = observe(tmp_path, JOB, job_body(model=model), job_setup(), row=False, name="refuse-job")
    assert obs.status == 404, obs.body
    assert '"code": "model_not_found"' in obs.body["text"]
    assert not [e for e in obs.engine if e[1] == "/prompt"]
    assert not media_jobs(obs)


def test_an_image_job_naming_a_generate_model_is_still_accepted(tmp_path):
    obs = observe(tmp_path, JOB, job_body(model="flux2-klein-4b"), job_setup(), row=False, name="accept-job")
    assert obs.status == 202, obs.body
