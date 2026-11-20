"""A workflow whose declared nodes or model files its engine does not list is not available: `required_file_missing`.

The judge is the owner's engine check (`workflow_requirements.judge_by_names`), by file name. The listing reads the
engine's node list from a kept copy (`gateway.engine_inventory`), never from the engine: these tests fill that copy
through its seam (`engine_inventory.store`) and no test here opens a connection.
"""

from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import availability, availability_reasons, engine_check, engine_inventory  # noqa: E402
from gateway.workflow_requirements import judge_by_names, validate_workflow_requirements  # noqa: E402

NOW = 1_700_000_000.0
ENGINE = "eng"
GRAPH_WORKFLOW = "image.generate.flux2-klein-snofs"   # a shipped manifest; its own requires block is what is judged


def _plain_workflow(records, engine):
    """A shipped workflow on the same engine whose bindings declare no files or nodes. It is chosen from the manifests, so a
    manifest that gains a requires block never turns this control into a judged workflow."""
    for workflow_id in sorted(records):
        record = records[workflow_id]
        bindings = record.contract.get("bindings") or {}
        if engine in record.supported_backends and isinstance(bindings, dict) and bindings and not any(
                isinstance(spec, dict) and spec.get("requires") for spec in bindings.values()):
            return workflow_id
    raise AssertionError("no shipped workflow on the engine declares nothing")

REQUIRES = {
    "custom_nodes": ["NodeA"],
    "models": [
        {"role": "diffusion_model", "path": "unet/model-q8.gguf"},
        {"role": "vae", "path": "vae/vae.safetensors"},
    ],
}


def node_list(classes=("KSampler", "NodeA"), **combos: list[str]) -> dict:
    """A node list as an engine answers it: each node with its inputs; a combo input offers file names, never a folder."""
    body = {name: {"input": {"required": {}}} for name in classes}
    for index, (input_name, names) in enumerate(combos.items()):
        body[f"Loader{index}"] = {"input": {"required": {input_name: [list(names), {}]}}}
    return body


def kept(body: dict, *, fetched_at: float = NOW, url: str = "") -> engine_inventory.NodeInventory:
    inventory = engine_inventory.from_node_list(body, fetched_at=fetched_at, url=url)
    assert inventory is not None
    return inventory


ALL_THERE = node_list(unet_name=["model-q8.gguf"], vae_name=["vae.safetensors"])


def record(requires: dict | None, *, backend: str = ENGINE, workflow_id: str = "wf"):
    contract = {"bindings": {backend: {"requires": requires}}} if requires is not None else {"bindings": {backend: {}}}
    return SimpleNamespace(id=workflow_id, contract=contract, supported_backends=[backend], manifest={})


@pytest.fixture(autouse=True)
def clean_cache():
    engine_inventory.reset()
    yield
    engine_inventory.reset()


# ----------------------------------------------------------------------------------------------- the judge, by name


def test_everything_present_lacks_nothing():
    assert availability.required_files_missing(REQUIRES, kept(ALL_THERE)) == []


def test_a_missing_node_is_named():
    body = node_list(classes=("KSampler",), unet_name=["model-q8.gguf"], vae_name=["vae.safetensors"])
    assert availability.required_files_missing(REQUIRES, kept(body)) == [{"kind": "custom_node", "name": "NodeA"}]


def test_a_missing_model_file_is_named_by_role_and_file():
    body = node_list(unet_name=["model-q8.gguf"], vae_name=["another.safetensors"])
    assert availability.required_files_missing(REQUIRES, kept(body)) == [{"kind": "model", "name": "vae:vae.safetensors"}]


@pytest.mark.parametrize("listed", [
    "model-q8.gguf",                  # the node list offers the bare file name, not the manifest's `unet/` folder
    "unet_gguf/model-q8.gguf",        # the engine's folder differs from the manifest's folder prefix
    "nested\\other\\model-q8.gguf",   # a name written the way a Windows install writes it
])
def test_a_file_listed_under_another_folder_still_counts_as_present(listed):
    body = node_list(unet_name=[listed], vae_name=["vae.safetensors"])
    assert availability.required_files_missing(REQUIRES, kept(body)) == []


def test_a_name_that_only_looks_alike_is_missing():
    body = node_list(unet_name=["model-q8.gguf.part", "x-model-q8.gguf"], vae_name=["vae.safetensors"])
    assert availability.required_files_missing(REQUIRES, kept(body)) == [{"kind": "model", "name": "diffusion_model:model-q8.gguf"}]


def test_a_pinned_hash_counts_as_present_by_name_and_absent_by_name_is_missing():
    # A list of names cannot check a hash (the engine check calls such a file "not confirmed"). The hash alone never marks
    # a workflow; a name the list does not offer still does.
    pinned = {"models": [{"role": "vae", "path": "vae/vae.safetensors", "sha256": "ab" * 32}]}
    assert availability.required_files_missing(pinned, kept(node_list(vae_name=["vae.safetensors"]))) == []
    assert availability.required_files_missing(pinned, kept(node_list(vae_name=["other.safetensors"]))) == [
        {"kind": "model", "name": "vae:vae.safetensors"}]
    # The engine check keeps its own, stricter words for the same file.
    offerings = engine_check.workflow_offerings(
        [{"id": "w", "bindings": {"b": {"requires": pinned}}}], "b", ["KSampler"], ["vae.safetensors"])
    assert offerings["unavailable"][0]["missing"][0]["reason"].startswith("matching model artifact with expected hash")


def test_a_pattern_requirement_matches_any_listed_name():
    requires = {"models": [{"role": "lora", "pattern": "*.lora.safetensors"}]}
    assert availability.required_files_missing(requires, kept(node_list(lora_name=["a/b.lora.safetensors"]))) == []
    assert availability.required_files_missing(requires, kept(node_list(lora_name=["b.safetensors"]))) == [
        {"kind": "model", "name": "lora:*.lora.safetensors"}]


def test_nothing_declared_and_unreadable_requirements_lack_nothing():
    inventory = kept(ALL_THERE)
    for requires in (None, {}, {"services": ["x"]}, {"models": [{"role": "vae"}]}, {"models": "not a list"}):
        assert availability.required_files_missing(requires, inventory) == []


def test_the_filtered_judge_agrees_with_the_unfiltered_judge():
    """`judge_by_names` keeps only the names that can satisfy a requirement; the verdict is the one the cross product gives."""
    names = ["a/one.bin", "two.bin", "deep/er/three.bin", "one.bin.bak", "four\\one.bin"]
    for requires in (
        {"models": [{"role": "r", "path": "x/one.bin"}, {"role": "s", "path": "three.bin"}, {"role": "t", "path": "nine.bin"}]},
        {"models": [{"role": "r", "path": "x/one.bin"}, {"role": "s", "pattern": "t*.bin"}]},
        {"custom_nodes": ["N"], "models": [{"role": "r", "path": "deep/er/three.bin"}]},
    ):
        names_clean = [n.replace("\\", "/") for n in names]
        roles = {m["role"] for m in requires.get("models", [])}
        full = {"node_classes": ["K"],
                "models": [{"role": r, "path": n, "name": n.rsplit("/", 1)[-1]} for r in sorted(roles) for n in names_clean]}
        expected = validate_workflow_requirements({"requires": requires}, full).missing
        assert judge_by_names(requires, ["K"], names_clean).missing == expected


# ------------------------------------------------------------------------------- the owner's workflow view


def lacking_row(**kw):
    return availability.workflow_availability(
        record(REQUIRES), service_records={"services": {ENGINE: {"enabled": True, "config": {}}}},
        evidence={ENGINE: {"status": "ok", "available": True, "observed_at_s": 900, "last_success_at_s": 900}}, **kw)


def test_the_owner_view_says_what_is_missing_beside_the_closed_code():
    body = node_list(classes=("KSampler",), unet_name=["model-q8.gguf"], vae_name=["vae.safetensors"])
    row = lacking_row(inventories={ENGINE: kept(body)})
    assert row["available"] is False
    assert row["reason_code"] == "required_file_missing"
    assert row["reason_code"] in availability_reasons.REASONS
    assert row["missing"] == [{"kind": "custom_node", "name": "NodeA"}]
    assert "NodeA" in row["reason"]
    assert row["last_healthy_at"] == 900


def test_the_owner_view_is_available_when_all_is_there():
    row = lacking_row(inventories={ENGINE: kept(ALL_THERE)})
    assert row["available"] is True and "reason_code" not in row and "missing" not in row


@pytest.mark.parametrize("inventories", [None, {}, {"another-engine": None}])
def test_the_owner_view_fails_open_without_a_list(inventories):
    body = node_list(classes=("KSampler",))
    assert lacking_row(inventories=inventories) == lacking_row()
    assert lacking_row(inventories={ENGINE: kept(body)})["available"] is False   # the same engine with a list is judged


def test_a_down_engine_keeps_its_own_reason_even_when_files_are_missing():
    body = node_list(classes=("KSampler",))
    down = availability.workflow_availability(
        record(REQUIRES), service_records={"services": {ENGINE: {"enabled": True, "name": "Engine", "config": {}}}},
        evidence={ENGINE: {"status": "down", "available": False, "observed_at_s": 900}},
        inventories={ENGINE: kept(body)})
    assert down["available"] is False
    assert "reason_code" not in down and "missing" not in down
    assert down["reason"] == "Engine is down."


def test_a_workflow_that_can_run_on_two_engines_is_lacking_only_when_both_lack():
    two = SimpleNamespace(id="wf", supported_backends=["a", "b"], manifest={}, contract={"bindings": {
        "a": {"requires": REQUIRES}, "b": {"service": "b-service", "requires": REQUIRES}}})
    services = {"services": {sid: {"enabled": True, "config": {}} for sid in ("a", "b-service")}}
    evidence = {sid: {"status": "ok", "available": True, "observed_at_s": 900} for sid in ("a", "b-service")}
    lacks = kept(node_list(classes=("KSampler",)))

    def row(inventories):
        return availability.workflow_availability(two, service_records=services, evidence=evidence, inventories=inventories)

    assert row({"a": lacks, "b-service": lacks})["reason_code"] == "required_file_missing"
    assert row({"a": lacks, "b-service": kept(ALL_THERE)})["available"] is True
    assert row({"a": lacks})["available"] is True       # the other engine has no list: not judged


# ------------------------------------------------------------------------------- the model list


def entry_state(workflow, *, inventories, evidence=None, engine=ENGINE, fresh=True):
    services = {"services": {engine: {"enabled": True, "config": {}}}}
    return proxy._model_entry_availability(
        {"id": "m", "azizam_workflow_id": workflow.id}, by_name={}, workflows={workflow.id: workflow},
        workflow_ids_by_model={}, service_records=services,
        evidence=evidence if evidence is not None else {engine: {"status": "up"}}, snapshot_fresh=fresh,
        inventories=inventories)


def test_the_model_list_names_the_reason_and_what_is_missing():
    body = node_list(classes=("KSampler",), unet_name=["model-q8.gguf"])
    state = entry_state(record(REQUIRES), inventories={ENGINE: kept(body)})
    assert state == {"state": "unavailable", "reason": "required_file_missing", "missing": [
        {"kind": "custom_node", "name": "NodeA"}, {"kind": "model", "name": "vae:vae.safetensors"}]}
    assert state["reason"] in availability_reasons.REASONS


def test_the_model_list_is_available_when_all_is_there_and_when_no_list_is_known():
    assert entry_state(record(REQUIRES), inventories={ENGINE: kept(ALL_THERE)}) == {"state": "available"}
    for inventories in (None, {}):
        assert entry_state(record(REQUIRES), inventories=inventories) == {"state": "available"}


def test_a_down_engine_keeps_backend_unavailable_in_the_model_list():
    body = node_list(classes=("KSampler",))
    state = entry_state(record(REQUIRES), inventories={ENGINE: kept(body)}, evidence={ENGINE: {"status": "down"}})
    assert state == {"state": "unavailable", "reason": "backend_unavailable"}


def test_an_engine_that_is_not_measured_keeps_its_unknown_state():
    body = node_list(classes=("KSampler",))
    assert entry_state(record(REQUIRES), inventories={ENGINE: kept(body)}, fresh=False) == {
        "state": "unknown", "reason": "health_unknown"}


def test_a_workflow_with_no_requirements_is_never_marked():
    body = node_list(classes=("KSampler",))
    assert entry_state(record(None), inventories={ENGINE: kept(body)}) == {"state": "available"}


# -------------------------------------------------------- the real projection functions, with the cache seam only


@pytest.fixture
def listing(monkeypatch):
    """`proxy._annotate_model_availability` over the shipped workflows with a registry that names the engine, and no
    network: the kept node list is the only thing that says what the engine holds."""
    records = {r.id: r for r in proxy.GATEWAY_WORKFLOW_REGISTRY.records()}
    engine = next(iter(records[GRAPH_WORKFLOW].supported_backends))
    plain = _plain_workflow(records, engine)
    registry = {"services": {engine: {"enabled": True, "config": {}}}}
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: registry)
    monkeypatch.setattr(proxy, "_operator_service_availability_overrides", lambda: {})
    monkeypatch.setattr(proxy, "_model_service_ids_index", lambda _registry: {})
    monkeypatch.setattr(engine_inventory, "fetch_node_list", lambda _source: (_ for _ in ()).throw(AssertionError("fetch")))
    snapshot = {"generated_at": 1, "backends": [{"service_id": engine, "status": "up"}]}

    def run(entries=None):
        data = entries or [{"id": "graph-model", "azizam_workflow_id": GRAPH_WORKFLOW},
                           {"id": "plain-model", "azizam_workflow_id": plain}]
        return {item["id"]: item.get("azizam_availability")
                for item in proxy._annotate_model_availability(copy.deepcopy(data), only_available=False,
                                                               status_payload=snapshot)}

    return SimpleNamespace(engine=engine, records=records, run=run, snapshot=snapshot, plain=plain)


def live_shaped(engine_names: dict[str, list[str]] | None = None, *, with_node: bool = True) -> dict:
    """The names the live engine's node list offers for the FLUX.2 Klein workflows: the diffusion model only under the
    loader class that reads a folder other than the manifest's, as bare file names."""
    classes = ("KSampler", "RMBG") if with_node else ("KSampler",)
    return node_list(classes, unet_name=["flux-2-klein-9b-Q8_0.gguf"], clip_name=["qwen_3_8b_fp8mixed.safetensors"],
                     vae_name=["flux2-vae.safetensors"], lora_name=["klein_snofs_v1_4.safetensors"],
                     **(engine_names or {}))


def test_both_klein_workflows_stay_available_with_a_live_shaped_list(listing):
    engine_inventory.store(listing.engine, kept(live_shaped(), fetched_at=__import__("time").time()))
    assert listing.run() == {"graph-model": {"state": "available"}, "plain-model": {"state": "available"}}
    row = availability.workflow_availability(
        listing.records[GRAPH_WORKFLOW], service_records={"services": {listing.engine: {"enabled": True, "config": {}}}},
        evidence={listing.engine: {"status": "ok", "available": True}}, records=listing.records,
        inventories=engine_inventory.fresh_all())
    assert row["available"] is True


@pytest.mark.parametrize("what", ["node", "model"])
def test_the_shipped_workflow_lists_as_missing_through_both_real_projections(listing, what):
    import time
    body = live_shaped(with_node=False) if what == "node" else node_list(
        ("KSampler", "RMBG"), unet_name=["flux-2-klein-9b-Q8_0.gguf"], clip_name=["qwen_3_8b_fp8mixed.safetensors"],
        vae_name=["flux2-vae.safetensors"])
    engine_inventory.store(listing.engine, kept(body, fetched_at=time.time()))
    expected = {"kind": "custom_node", "name": "RMBG"} if what == "node" else {"kind": "model", "name": "lora:klein_snofs_v1_4.safetensors"}

    listed = listing.run()
    assert listed["graph-model"]["state"] == "unavailable"
    assert listed["graph-model"]["reason"] == "required_file_missing"
    assert listed["graph-model"]["missing"] == [expected]
    assert listed["plain-model"] == {"state": "available"}            # declares nothing, so nothing is lacking

    # `?available=true` leaves it out, as for any other unavailable name
    kept_ids = [i["id"] for i in proxy._annotate_model_availability(
        [{"id": "graph-model", "azizam_workflow_id": GRAPH_WORKFLOW}], only_available=True, status_payload=listing.snapshot)]
    assert kept_ids == []

    owner = asyncio.run(_owner_view(listing))
    assert owner[GRAPH_WORKFLOW]["available"] is False
    assert owner[GRAPH_WORKFLOW]["reason_code"] == "required_file_missing"
    assert owner[GRAPH_WORKFLOW]["missing"] == [expected]


async def _owner_view(listing):
    """`proxy._mcp_availability`, the owner workflow view, with the engine's health read from the snapshot only."""
    async def evidence(_capabilities, _service_records):
        return {listing.engine: {"status": "ok", "available": True, "observed_at_s": 900, "last_success_at_s": 900}}

    with patch.object(proxy, "_gateway_capabilities_for_auth_context", lambda _auth: []), \
            patch.object(proxy, "_auth_context_from_scope", lambda _request: None), \
            patch.object(proxy, "_capabilities_availability_evidence", evidence):
        return await proxy._mcp_availability(SimpleNamespace(), [GRAPH_WORKFLOW, listing.plain])


def test_a_stale_list_and_an_unreadable_list_leave_the_listing_as_it_was(listing):
    import time
    baseline = listing.run()
    lacking = kept(live_shaped(with_node=False), fetched_at=time.time(), url="http://engine.invalid/nodes")
    # stale: read longer ago than the limit
    engine_inventory.store(listing.engine, kept(
        live_shaped(with_node=False), fetched_at=time.time() - engine_inventory.MAX_AGE_S - 1))
    assert listing.run() == baseline
    # a fresh list does mark it, so the two cases above were the age
    engine_inventory.store(listing.engine, lacking)
    assert listing.run()["graph-model"]["reason"] == "required_file_missing"
    # a read that fails after that keeps the earlier list until it is stale, then the listing is as it was
    report = engine_inventory.refresh_all({listing.engine: engine_inventory.Source("http://engine.invalid/nodes")},
                                          fetch=lambda _source: (_ for _ in ()).throw(OSError("down")))
    assert report == {listing.engine: "OSError"}
    assert listing.run()["graph-model"]["reason"] == "required_file_missing"
    engine_inventory.reset()
    assert listing.run() == baseline


def test_a_down_engine_keeps_its_reason_through_the_real_listing(listing):
    import time
    engine_inventory.store(listing.engine, kept(live_shaped(with_node=False), fetched_at=time.time()))
    listing.snapshot["backends"][0]["status"] = "down"
    assert listing.run()["graph-model"] == {"state": "unavailable", "reason": "backend_unavailable"}


# ------------------------------------------------------------------ the kept lists: never a fetch on a listing


def test_a_list_that_is_not_a_node_list_is_not_kept():
    for body in (None, [], {}, {"error": "x"}, {"Node": {"no_input": 1}}, "text"):
        assert engine_inventory.from_node_list(body, fetched_at=NOW) is None
    report = engine_inventory.refresh_all({"e": engine_inventory.Source("http://engine.invalid/n")},
                                          fetch=lambda _s: {"error": "not a list"}, now=NOW)
    assert report == {"e": "ValueError"} and engine_inventory.fresh_all(now=NOW) == {}


def test_the_age_limit_and_a_clock_that_moved_back():
    engine_inventory.store("e", kept(ALL_THERE, fetched_at=NOW))
    assert engine_inventory.fresh("e", now=NOW + engine_inventory.MAX_AGE_S - 1) is not None
    assert engine_inventory.fresh("e", now=NOW + engine_inventory.MAX_AGE_S) is None
    assert engine_inventory.fresh("e", now=NOW - 5) is None
    assert engine_inventory.fresh("missing", now=NOW) is None


def test_refresh_keeps_an_earlier_list_on_failure_and_drops_a_moved_or_removed_engine():
    source = engine_inventory.Source("http://one.invalid/nodes")
    assert engine_inventory.refresh_all({"e": source}, fetch=lambda _s: ALL_THERE, now=NOW) == {"e": None}
    assert engine_inventory.fresh("e", now=NOW) is not None
    engine_inventory.refresh_all({"e": source}, fetch=lambda _s: (_ for _ in ()).throw(TimeoutError()), now=NOW + 1)
    assert engine_inventory.fresh("e", now=NOW + 1) is not None
    # the address changed: the old engine's list is not the new one's
    moved = engine_inventory.Source("http://two.invalid/nodes")
    engine_inventory.refresh_all({"e": moved}, fetch=lambda _s: (_ for _ in ()).throw(TimeoutError()), now=NOW + 2)
    assert engine_inventory.fresh("e", now=NOW + 2) is None
    engine_inventory.refresh_all({"e": source}, fetch=lambda _s: ALL_THERE, now=NOW + 3)
    engine_inventory.refresh_all({}, fetch=lambda _s: ALL_THERE, now=NOW + 4)
    assert engine_inventory.fresh_all(now=NOW + 4) == {}


def test_the_loop_reads_in_the_background_and_stops():
    async def go():
        stop, seen = asyncio.Event(), []

        def fetch(source):
            seen.append(source.url)
            return ALL_THERE

        task = asyncio.create_task(engine_inventory.run_loop(
            stop, lambda: {"e": engine_inventory.Source("http://e.invalid/n")}, fetch=fetch, every_s=0.01, first_after_s=0.0))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(seen) >= 2:
                break
        stop.set()
        await asyncio.wait_for(task, 2)
        return seen

    assert len(asyncio.run(go())) >= 2
    assert engine_inventory.fresh("e") is not None


def test_where_a_list_is_read_is_the_engine_definitions_data():
    probe = engine_inventory.node_list_probe("node-graph")
    assert probe is not None and probe["offers"] == "node"
    assert engine_inventory.node_list_probe("openai-compatible") is None
    sources = engine_inventory.sources_for([("g", "node-graph", "http://host.invalid:1234/prefix"),
                                            ("o", "openai-compatible", "http://other.invalid"),
                                            ("n", "node-graph", "")])
    assert sources == {"g": engine_inventory.Source("http://host.invalid:1234" + probe["path"], probe["cap_bytes"])}


def test_the_gateway_names_its_graph_engines_sources_from_the_registry(monkeypatch):
    sources = proxy._engine_inventory_sources()
    assert all(isinstance(key, str) and source.url.startswith("http") for key, source in sources.items())


def test_the_closed_set_holds_the_code_and_the_doc_lists_it():
    assert "required_file_missing" in availability_reasons.REASONS
    text = (ROOT / "docs" / "api" / "MODEL_METADATA.md").read_text()
    assert "`required_file_missing`" in text.split("### Reason codes", 1)[1]
