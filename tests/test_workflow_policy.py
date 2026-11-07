"""Policy facts are record data, with no defaults; a workflow without them needs configuration (CODE_TO_DATA_S3, D2, S3.5a).

Gate controls of the S3.5 row: removing ``capability`` from a manifest is refused, not inferred; removing one workflow's
facts gives "needs configuration" and a run makes zero engine calls; the image route's policy build, run on a valid
contract wrapped in a mapping that records key reads, fails if any policy fact is read from the contract.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from gateway import capabilities
from gateway.discovery import WorkflowRegistry, summarize_workflow
from gateway.manifests import ContractErrors, Provenance, load_workflow_manifest, manifest_identity
from gateway.workflow_policy import (
    NEEDS_CONFIGURATION_CODE,
    NeedsConfiguration,
    PolicyFacts,
    PolicyFileError,
    facts_from_mapping,
    parse_policy_text,
    release_facts,
    release_policy,
)
from tests.support.access import access_context
from tests.support.config_snapshot import derived_patch
from tests.support.fake_upstream.async_client_stub import install_async_client_stub
from tests.test_workflow_readers_v2 import POLICY_KEYS_AT_TOP, Recording

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "manifests" / "workflows"
V1 = ROOT / "tests" / "fixtures" / "workflows_v1"
TILE = "image.generate.flux2-klein-4b-seamless-tile"
GOOD = {"exposure": "default", "trust_tier": "stable", "resource_tier": "batch", "tags": []}


def policy_text(rows: dict[str, Any]) -> str:
    return yaml.safe_dump({"schema_version": 1, "workflows": rows})


# ------------------------------------------------------------------------------------------------------ the file


DEMO = "image.generate.test-demo-package"


def test_the_release_file_states_the_facts_of_every_bundled_workflow_and_the_demo_package_and_nothing_else():
    bundled = {yaml.safe_load(path.read_text(encoding="utf-8"))["id"] for path in BUNDLED.glob("*.yml")}
    policy = release_policy()
    assert set(policy) == bundled | {DEMO} and len(policy) == 39
    assert {facts.exposure for key, facts in policy.items() if key != DEMO} == {"default"}
    assert {(facts.trust_tier, facts.resource_tier, facts.tags) for key, facts in policy.items() if key != DEMO} == {("stable", "batch", ())}
    # The shipped demo package keeps the facts its schema 1 file stated, so it stays runnable with the same exposure.
    assert policy[DEMO] == PolicyFacts("opt_in", "experimental", "batch", ("test", "demo"))


def test_the_facts_equal_what_the_old_readers_gave_a_bundled_document_that_stated_none():
    # The bundled v1 files stated none of the four (the readers filled them in); the file records today's effective values.
    for path in V1.glob("*.yml"):
        v1 = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert not {"exposure", "trust_tier", "resource_tier", "tags"} & set(v1), path.name


def test_a_fact_that_is_missing_or_malformed_is_not_filled_in():
    for broken in ({**GOOD, "exposure": None}, {**GOOD, "tags": "a"}, {**GOOD, "tags": ["a", "a"]}, {**GOOD, "trust_tier": ""}):
        assert facts_from_mapping(broken) is None
    for name in GOOD:
        assert facts_from_mapping({key: value for key, value in GOOD.items() if key != name}) is None
    assert facts_from_mapping(GOOD) == PolicyFacts("default", "stable", "batch", ())
    assert facts_from_mapping(None) is None and facts_from_mapping([]) is None


@pytest.mark.parametrize("text, message", [
    ("- a", "exactly schema_version and workflows"),
    ("schema_version: 2\nworkflows: {}", "unsupported schema_version"),
    ("schema_version: 1\nworkflows: []", "mapping"),
    (policy_text({"w": {"exposure": "default"}}), "must state exactly"),
    (policy_text({"w": {**GOOD, "extra": 1}}), "must state exactly"),
    (policy_text({"w": {**GOOD, "tags": [1]}}), "not valid text"),
    ("a: [", "does not parse"),
])
def test_a_policy_file_that_breaks_its_rules_does_not_load(text, message):
    with pytest.raises(PolicyFileError, match=message):
        parse_policy_text(text)


def test_a_missing_or_broken_file_states_nothing_so_every_workflow_needs_configuration(tmp_path):
    assert release_facts("w", tmp_path / "absent.yml") is None
    broken = tmp_path / "broken.yml"
    broken.write_text("nonsense: 1", encoding="utf-8")
    assert release_facts("w", broken) is None
    with pytest.raises(PolicyFileError):
        release_policy(tmp_path / "absent.yml")
    good = tmp_path / "good.yml"
    good.write_text(policy_text({"w": GOOD}), encoding="utf-8")
    assert release_facts("w", good) == PolicyFacts("default", "stable", "batch", ())
    good.write_text(policy_text({"w": {**GOOD, "exposure": "admin"}}), encoding="utf-8")
    assert release_facts("w", good).exposure == "admin"   # a changed file loads again


# ------------------------------------------------------------------------------------------ provenance, not document


def test_trusted_and_source_are_set_by_the_loader_not_read_from_the_document():
    registry = WorkflowRegistry.from_directory(BUNDLED)
    assert {(record.identity.trusted, record.identity.source) for record in registry.records()} == {(True, "local")}
    document = copy.deepcopy(registry.get(TILE).manifest)
    assert not {"trusted", "source"} & set(document)
    assert manifest_identity(document).trusted is False and manifest_identity(document).source is None


# --------------------------------------------------------------------------------------------- needs configuration


def registry_without_facts_for(workflow_id: str, tmp_path: Path) -> WorkflowRegistry:
    policy = {key: dataclasses.asdict(value) for key, value in release_policy().items() if key != workflow_id}
    for row in policy.values():
        row["tags"] = list(row["tags"])
    path = tmp_path / "policy.yml"
    path.write_text(policy_text(policy), encoding="utf-8")
    registry = WorkflowRegistry()
    registry.load_directory(BUNDLED, policy=release_policy(path))
    return registry


def test_control_a_workflow_without_facts_needs_configuration_in_every_reader(tmp_path):
    registry = registry_without_facts_for(TILE, tmp_path)
    record = registry.get(TILE)
    assert record.facts is None and record.needs_configuration
    assert all(other.facts is not None for other in registry.records() if other.id != TILE)
    summary = summarize_workflow(record, "internal")
    assert summary["needs_configuration"] is True
    assert (summary["exposure"], summary["trust_tier"], summary["resource_tier"], summary["tags"]) == (None, None, None, [])
    (cap,) = [capabilities.capability_from_workflow_record(record, backend=backend) for backend in record.supported_backends]
    assert cap.exposure == "disabled" and any("needs configuration" in caveat for caveat in cap.caveats)
    stated = summarize_workflow(registry.get("image.generate.flux2-klein"), "internal")
    assert "needs_configuration" not in stated and stated["exposure"] == "default"


class JsonRequest:
    headers: dict[str, str] = {}
    url = SimpleNamespace(scheme="http", netloc="gateway.test")

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


def test_control_a_run_of_a_workflow_without_facts_makes_zero_engine_calls():
    import proxy

    record = proxy.GATEWAY_WORKFLOW_REGISTRY.get(TILE)
    counts = {"posts": 0, "rounds": 0}

    async def counting_post(*_a: Any, **_k: Any) -> Any:
        counts["posts"] += 1
        raise AssertionError("an engine was called")

    async def counting_round(*_a: Any, **_k: Any) -> Any:
        counts["rounds"] += 1
        raise AssertionError("an engine was called")

    def run() -> Any:
        receipt = proxy.start_image_route_receipt("/v1/images/generations", access_context())
        body = {"model": TILE, "prompt": "painted rock texture", "size": "64x64", "n": 1, "response_format": "b64_json"}
        with pytest.MonkeyPatch.context() as mp, derived_patch("COMFY_ENABLED", True), patch.object(
            proxy, "configured_downstream_headers", lambda _auth, _service: {}
        ), patch.object(proxy, "_graph_run_round", counting_round):
            install_async_client_stub(mp, counting_post)
            return asyncio.run(proxy.proxy_graph_image_request(JsonRequest(body), access_context(), receipt=receipt))

    proxy.GATEWAY_WORKFLOW_REGISTRY.add_manifest(record.manifest, replace=True, provenance=Provenance(record.identity.trusted, record.identity.source), facts=None)
    try:
        response = run()
    finally:
        proxy.GATEWAY_WORKFLOW_REGISTRY.add_manifest(record.manifest, replace=True, provenance=Provenance(record.identity.trusted, record.identity.source), facts=record.facts)
    # The closed record is not an available model: the route answers before it resolves a service, let alone calls one.
    assert response.status_code == 404 and json.loads(response.body)["error"]["code"] == "model_not_found"
    assert counts == {"posts": 0, "rounds": 0}


def test_the_image_route_policy_denies_a_workflow_without_facts_with_the_needs_configuration_code():
    import proxy

    record = proxy.GATEWAY_WORKFLOW_REGISTRY.get(TILE)
    receipt = proxy.start_image_route_receipt("/v1/images/generations", access_context())
    decision = proxy._apply_image_route_policy(receipt, access_context(), workflow=TILE, manifest={"id": "no.such.facts"})
    assert decision.denied and decision.code == NEEDS_CONFIGURATION_CODE
    assert decision.sources == ("policy:workflow-needs-configuration",)
    receipt = proxy.start_image_route_receipt("/v1/images/generations", access_context())
    assert not proxy._apply_image_route_policy(receipt, access_context(), workflow=TILE, manifest=record.manifest).denied


# ----------------------------------------------------------------------------------------------- the key-read test


def policy_reads(build) -> set[str]:
    """The top-level keys of the contract that a policy build reads."""
    import proxy

    reads: list[tuple[bool, str]] = []
    record = proxy.GATEWAY_WORKFLOW_REGISTRY.get(TILE)
    build(Recording(record.manifest, reads))
    return {key for top, key in reads if top}


def test_the_image_route_policy_build_reads_no_policy_fact_from_the_contract():
    import proxy

    reads = policy_reads(proxy._image_manifest_policy_metadata)
    assert not reads & POLICY_KEYS_AT_TOP and reads <= {"id"}


def test_control_putting_back_the_manifest_read_makes_the_key_read_test_fail():
    """The old build took the facts from the document. Run through the same recording, it is caught."""
    import proxy

    def old_build(manifest):
        return {name: manifest.get(name) for name in ("exposure", "trust_tier", "resource_tier", "tags")} | {"trusted": manifest.get("trusted")}

    with patch.object(proxy, "_image_manifest_policy_metadata", old_build):
        reads = policy_reads(proxy._image_manifest_policy_metadata)
    assert reads & POLICY_KEYS_AT_TOP == {"exposure", "trust_tier", "resource_tier", "tags", "trusted"}


def test_the_policy_of_a_workflow_is_its_record_facts_and_a_model_request_has_its_own_answer():
    import proxy

    record = proxy.GATEWAY_WORKFLOW_REGISTRY.get(TILE)
    assert proxy._image_manifest_policy_metadata(record.manifest) == {
        "exposure": "default", "trust_tier": "stable", "resource_tier": "batch", "tags": ()}
    assert proxy._image_manifest_policy_metadata(None)["exposure"] == "default"
    with pytest.raises(NeedsConfiguration):
        proxy._image_manifest_policy_metadata({"id": "no.such.workflow"})


# ------------------------------------------------------------------------------------- capability and the version


def test_control_a_document_without_a_capability_is_refused_not_inferred(tmp_path):
    document = yaml.safe_load((BUNDLED / "flux2-klein.yml").read_text(encoding="utf-8"))
    assert document["capability"] == "image"
    del document["capability"]
    path = tmp_path / "flux2-klein.yml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    with pytest.raises(ContractErrors) as raised:
        load_workflow_manifest(path)
    assert [(error.path, error.code) for error in raised.value.errors] == [(("capability",), "required_missing")]
    registry = WorkflowRegistry.from_directory(tmp_path)
    assert registry.records() == [] and "missing the required field capability" in " ".join(e.message for e in registry.errors)


def test_the_catalog_has_no_inference_left_to_do():
    from gateway import workflows_catalog

    assert not hasattr(workflows_catalog, "_capability_from_intent")


def test_a_schema_one_file_is_refused_at_load_with_the_converter_command_named(tmp_path):
    shutil.copy(V1 / "flux2-klein.yml", tmp_path / "flux2-klein.yml")
    with pytest.raises(ContractErrors) as raised:
        load_workflow_manifest(tmp_path / "flux2-klein.yml")
    assert raised.value.code == "schema_version_unsupported"
    assert "workflow_contract.py convert" in str(raised.value)
    registry = WorkflowRegistry.from_directory(tmp_path)
    assert registry.records() == [] and "workflow_contract.py convert" in " ".join(e.message for e in registry.errors)


def test_the_loader_no_longer_converts_in_memory():
    from gateway import manifests

    assert not hasattr(manifests, "log_bundled_report")
    import gateway.workflow_convert as convert

    assert not hasattr(convert, "normalize") and callable(convert.convert_1_to_2)
