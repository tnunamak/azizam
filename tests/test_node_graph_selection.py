"""The graph adapter reads a workflow's results through its selectors (S2.4, D5).

The rules for missing and null, order, duplicates and lists each have a case. A binding with no selectors is refused: the
engine's answer is never read by guessing which files are the result.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from gateway import output_selectors
from gateway.engine_protocol import description, node_graph
from gateway.engine_protocol.errors import UpstreamError
from gateway.engine_protocol.node_graph import NodeGraphAdapter, SelectionRefused
from gateway.result_bundle import ResultLimits
from tests.support.fake_upstream import HandlerTransport, fake_upstreams  # noqa: F401 - fixture
from tests.test_node_graph_adapter import CONN, HOST

ADAPTER = NodeGraphAdapter(description.load_registry()["node-graph"])
LIMITS = ResultLimits()


def sel(node: str = "9", key: str = "images", take: str = "all", decode: str = "none") -> dict[str, str]:
    return {"node": node, "key": key, "from": "output_dir", "decode": decode, "take": take}


def file(name: str, kind: str | None = "output", folder: str = "") -> dict[str, Any]:
    entry: dict[str, Any] = {"filename": name, "subfolder": folder}
    if kind is not None:
        entry["type"] = kind
    return entry


def job(**nodes: dict[str, list]) -> dict[str, Any]:
    return {"status": {"completed": True}, "outputs": {node.removeprefix("n"): value for node, value in nodes.items()}}


def pick(history: dict[str, Any], **selectors: dict[str, str]) -> dict[str, list[str]]:
    chosen = node_graph.select_outputs(history, selectors)
    return {name: [f["filename"] for f in files] for name, files in chosen.items()}


def refused(history: Any, code: str, **selectors: dict[str, str]) -> str:
    with pytest.raises(SelectionRefused) as caught:
        node_graph.select_outputs(history, selectors)
    assert caught.value.code == code
    return str(caught.value)


# ----------------------------------------------------------------------------------------------- the selectors

def test_a_selector_reads_its_own_node_and_key_and_nothing_else():
    history = job(n8={"images": [file("other.png")]}, n9={"images": [file("a.png"), file("b.png")], "audio": [file("c.wav")]})
    assert pick(history, image=sel()) == {"image": ["a.png", "b.png"]}
    assert pick(history, sound=sel(key="audio")) == {"sound": ["c.wav"]}
    assert pick(history, image=sel(node="8")) == {"image": ["other.png"]}


def test_only_the_output_directory_counts_not_a_source_echo_or_a_preview():
    history = job(n9={"images": [file("echo.png", "input"), file("peek.png", "temp"), file("real.png", "output")]})
    assert pick(history, image=sel()) == {"image": ["real.png"]}
    refused(job(n9={"images": [file("echo.png", "input"), file("peek.png", "temp")]}), "result_missing", image=sel())


def test_an_entry_with_no_type_is_an_output_file():
    assert pick(job(n9={"images": [file("a.png", None)]}), image=sel()) == {"image": ["a.png"]}


@pytest.mark.parametrize("history", [
    {}, {"outputs": None}, {"outputs": []}, job(), job(n9=None), job(n9={}), job(n9={"images": None}),
    job(n9={"images": []}), job(n9={"audio": [file("a.wav")]}), job(n7={"images": [file("a.png")]}), None,
], ids=["empty", "null-outputs", "list-outputs", "no-nodes", "null-node", "no-keys", "null-list", "empty-list",
        "other-key", "other-node", "no-job"])
def test_missing_null_and_empty_are_all_the_same_missing_result(history: Any):
    message = refused(history, "result_missing", image=sel())
    assert "image" in message       # the refusal names the result


def test_a_missing_result_fails_the_whole_selection_so_a_partial_bundle_is_never_made():
    history = job(n9={"images": [file("a.png")]})
    refused(history, "result_missing", image=sel(), sound=sel(key="audio"))


def test_order_is_the_order_of_the_answer():
    history = job(n9={"images": [file("b.png"), file("a.png"), file("c.png")]})
    assert pick(history, image=sel()) == {"image": ["b.png", "a.png", "c.png"]}


def test_a_file_listed_twice_is_kept_once_at_its_first_place():
    history = job(n9={"images": [file("a.png"), file("b.png"), file("a.png"), file("a.png", folder="sub")]})
    assert pick(history, image=sel()) == {"image": ["a.png", "b.png", "a.png"]}     # the one in sub/ is another file
    assert [f["subfolder"] for f in node_graph.select_outputs(history, {"image": sel()})["image"]] == ["", "", "sub"]


def test_take_all_first_and_single():
    history = job(n9={"images": [file("a.png"), file("b.png")]}, n3={"images": [file("only.png")]})
    assert pick(history, r=sel(take="all")) == {"r": ["a.png", "b.png"]}
    assert pick(history, r=sel(take="first")) == {"r": ["a.png"]}
    assert pick(history, r=sel(node="3", take="single")) == {"r": ["only.png"]}
    refused(history, "result_ambiguous", r=sel(take="single"))


def test_single_counts_files_after_the_filters_and_the_duplicate_rule():
    history = job(n9={"images": [file("a.png"), file("a.png"), file("peek.png", "temp")]})
    assert pick(history, r=sel(take="single")) == {"r": ["a.png"]}


@pytest.mark.parametrize("entries", [
    "a.png", ["a.png"], [None], [file("../a.png")], [file("a/b.png")], [file("")], [file("a.png", folder="../up")],
    [file("a.png", folder="/abs")], [file("a.png", folder="a//b")], [{"subfolder": ""}], [{"filename": 7}],
    [file("a\\b.png")],
])
def test_a_malformed_list_or_entry_is_refused(entries: Any):
    refused(job(n9={"images": entries}), "result_malformed", image=sel())


def test_a_nested_subfolder_is_a_plain_path():
    assert pick(job(n9={"images": [file("a.mp4", folder="video/sub")]}), v=sel()) == {"v": ["a.mp4"]}


def test_an_entry_in_a_directory_other_than_output_is_dropped_before_it_is_checked():
    # A preview with an odd name is not a result, and it does not make the selection fail either.
    assert pick(job(n9={"images": [file("../x", "temp"), file("a.png")]}), image=sel()) == {"image": ["a.png"]}


OUTPUT = {"result": {"media_type": "a/b"}}


# ----------------------------------------------------------------------------------------------- collect()

BLOCK = {"workflow": "w.json", "selectors": {"result": sel(node="9", key="images", take="first")}}
HISTORY = job(n8={"images": [file("peek.png", "temp")]}, n9={"images": [file("a.png"), file("b.png")]})


def collect(block: Any = BLOCK, history: Any = HISTORY, limits: Any = LIMITS, **kw: Any):
    return ADAPTER.collect(history, block, OUTPUT, limits=limits, **kw)


def test_a_binding_with_selectors_is_read_by_them():
    chosen = collect()
    assert [f["filename"] for f in chosen["result"]] == ["a.png"]


def test_a_binding_with_no_selectors_is_refused_when_it_runs_whatever_the_engine_answered():
    block = {"workflow": "w.json"}
    history = job(n9={"images": [file("a.png")]})
    with pytest.raises(SelectionRefused) as caught:
        collect(block, history)
    assert caught.value.code == "selectors_missing"
    with pytest.raises(SelectionRefused) as caught:
        collect({}, history)
    assert caught.value.code == "selectors_missing"
    with pytest.raises(SelectionRefused) as caught:
        collect({"workflow": "w.json", "selectors": None}, history)
    assert caught.value.code == "selectors_missing"


def test_a_binding_whose_selectors_do_not_fit_the_descriptor_is_refused_when_it_runs():
    bad = {"selectors": {"result": {**sel(), "key": "unlisted"}}}
    with pytest.raises(SelectionRefused) as caught:
        collect(bad)
    assert caught.value.code == "selector_malformed"


def test_an_adapter_with_no_selector_descriptor_refuses_selectors():
    with pytest.raises(SelectionRefused) as caught:
        NodeGraphAdapter().collect(HISTORY, BLOCK, OUTPUT, limits=LIMITS)
    assert caught.value.code == "selectors_unsupported"


def test_the_selector_count_limit_is_enforced_when_the_results_are_read():
    outputs = {f"r{i}": {} for i in range(3)}
    block = {"selectors": {name: sel(node=str(i)) for i, name in enumerate(outputs)}}
    history = job(n0={"images": [file("a")]}, n1={"images": [file("b")]}, n2={"images": [file("c")]})
    assert len(ADAPTER.collect(history, block, outputs, limits=ResultLimits(max_selectors=3))) == 3
    with pytest.raises(SelectionRefused) as caught:
        ADAPTER.collect(history, block, outputs, limits=ResultLimits(max_selectors=2))
    assert caught.value.code == "selector_count"


def test_a_history_answer_over_the_byte_limit_is_refused_before_anything_is_read():
    small = ResultLimits(max_history_bytes=100)
    assert collect(limits=small, history_bytes=100)
    with pytest.raises(SelectionRefused) as caught:
        collect(limits=small, history_bytes=101)
    assert caught.value.code == "history_too_large"
    with pytest.raises(SelectionRefused) as caught:
        collect({"workflow": "w"}, HISTORY, small, history_bytes=101)    # the byte limit is checked before the binding is
    assert caught.value.code == "history_too_large"


def test_the_limits_for_fetching_come_from_trusted_config_with_code_defaults():
    from gateway import result_bundle

    defaults = result_bundle.limits_from_config({})
    assert (defaults.max_selectors, defaults.max_history_bytes, defaults.fetch_timeout_s, defaults.max_concurrent_fetches) == (
        16, 8 * 1024 * 1024, 300, 8)
    tuned = result_bundle.limits_from_config({"workflow_execution": {"result_limits": {
        "max_selectors": 3, "max_history_bytes": 10, "fetch_timeout_s": 7, "max_concurrent_fetches": 2}}})
    assert (tuned.max_selectors, tuned.max_history_bytes, tuned.fetch_timeout_s, tuned.max_concurrent_fetches) == (3, 10, 7, 2)


# ------------------------------------------------------------------------------------------------- JSON files

def serve(fakes: Any, handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    fakes.router.add_transport(HOST, HandlerTransport(record))
    return seen


def test_a_json_result_file_is_read_within_the_byte_limit(fake_upstreams: Any):
    seen = serve(fake_upstreams, lambda r: httpx.Response(200, content=b'{"a": 1}'))
    got = asyncio.run(ADAPTER.fetch_json(CONN, {"filename": "r.json", "subfolder": "s", "type": "output"}, max_bytes=100))
    assert got == b'{"a": 1}'
    assert dict(seen[0].url.params) == {"filename": "r.json", "type": "output", "subfolder": "s"}
    with pytest.raises(SelectionRefused) as caught:
        asyncio.run(ADAPTER.fetch_json(CONN, {"filename": "r.json"}, max_bytes=3))
    assert caught.value.code == "json_too_large"


def test_a_json_result_file_that_cannot_be_read_is_an_upstream_failure(fake_upstreams: Any):
    serve(fake_upstreams, lambda r: httpx.Response(404))
    with pytest.raises(UpstreamError):
        asyncio.run(ADAPTER.fetch_json(CONN, {"filename": "r.json"}, max_bytes=100))


def test_a_selector_yields_the_kind_its_decode_names():
    descriptor = ADAPTER.result_selectors
    assert descriptor.kind_of(output_selectors.normalize(descriptor, "r", {"node": "1", "key": "images", "from": "output_dir"})) == "assets"
    assert descriptor.kind_of(output_selectors.normalize(
        descriptor, "r", {"node": "1", "key": "images", "from": "output_dir", "decode": "json"})) == "json"


# ------------------------------------------------------------------------------- count after selection (S3.8, D5)

def counted(**count: int) -> dict[str, Any]:
    return {"result": {"media_type": "a/b", "count": count}}


def collect_counted(count: dict[str, Any], block: Any, history: Any, limits: Any = LIMITS):
    return ADAPTER.collect(history, block, count, limits=limits)


def selecting(take: str = "all", decode: str = "none") -> dict[str, Any]:
    return {"workflow": "w.json", "selectors": {"result": sel(take=take, decode=decode)}}


def test_a_graph_result_is_counted_after_filtering_and_deduplication():
    block = selecting("all")
    once = job(n9={"images": [file("a.png"), file("a.png"), file("peek.png", "temp"), file("echo.png", "input")]})
    with pytest.raises(SelectionRefused) as caught:
        collect_counted(counted(min=2, max=3), block, once)     # five entries, one retained file
    assert caught.value.code == "result_count"
    twice = job(n9={"images": [file("a.png"), file("a.png"), file("b.png"), file("peek.png", "temp")]})
    chosen = collect_counted(counted(min=2, max=3), block, twice)
    assert [f["filename"] for f in chosen["result"]] == ["a.png", "b.png"]     # order is the answer's, each file once


def test_a_graph_result_above_its_count_is_refused_before_anything_is_fetched():
    history = job(n9={"images": [file("a.png"), file("b.png"), file("c.png")]})
    with pytest.raises(SelectionRefused) as caught:
        collect_counted(counted(min=1, max=2), selecting("all"), history)
    assert caught.value.code == "result_count"
    assert len(collect_counted(counted(min=1, max=3), selecting("all"), history)["result"]) == 3


def test_take_first_counts_the_selected_file_not_the_raw_list():
    history = job(n9={"images": [file("a.png"), file("b.png")]})
    with pytest.raises(SelectionRefused) as caught:          # a save refuses this; the run defends itself too
        collect_counted(counted(min=2, max=3), selecting("first"), history)
    assert caught.value.code == "result_count"
    chosen = collect_counted(counted(min=1, max=1), selecting("first"), history)
    assert [f["filename"] for f in chosen["result"]] == ["a.png"]


def test_the_default_count_is_the_limit_of_the_deployment():
    history = job(n9={"images": [file("a.png"), file("b.png"), file("c.png")]})
    assert len(collect_counted({"result": {"media_type": "a/b"}}, selecting("all"), history, ResultLimits(max_assets=3))["result"]) == 3
    with pytest.raises(SelectionRefused) as caught:
        collect_counted({"result": {"media_type": "a/b"}}, selecting("all"), history, ResultLimits(max_assets=2))
    assert caught.value.code == "result_count"


@pytest.mark.parametrize("take", ["all", "first", "single"])
@pytest.mark.parametrize("decode", ["none", "json"])
def test_an_absent_count_keeps_each_selector_style(take: str, decode: str):
    history = job(n9={"images": [file("a.png")]})
    chosen = collect_counted({"result": {"media_type": "a/b"}}, selecting(take, decode), history)
    assert [f["filename"] for f in chosen["result"]] == ["a.png"]


def test_an_absent_count_keeps_the_selection_refusals_in_their_order():
    two = job(n9={"images": [file("a.png"), file("b.png")]})
    block = {"result": {"media_type": "a/b"}}
    with pytest.raises(SelectionRefused) as caught:
        collect_counted(block, selecting("single"), two)
    assert caught.value.code == "result_ambiguous"            # before the count, as before
    with pytest.raises(SelectionRefused) as caught:
        collect_counted(counted(min=2), selecting("all"), job(n9={"images": []}))
    assert caught.value.code == "result_missing"              # an empty list is missing, not a short count
    assert len(collect_counted(block, selecting("all", "json"), two)["result"]) == 2   # two JSON files: ``results`` says ambiguous


def test_json_selectors_advertise_one_document_even_with_take_all():
    assert ADAPTER.selector_cardinality(sel(take="all", decode="json")) == (1, 1)
    assert ADAPTER.selector_cardinality(sel(take="all")) == (1, None)
    assert ADAPTER.selector_cardinality(sel(take="first")) == (1, 1)
    assert ADAPTER.selector_cardinality(sel(take="single")) == (1, 1)


def test_the_adapter_hook_is_the_one_the_save_path_finds():
    from gateway import binding_registry

    assert binding_registry.selector_cardinality("node-graph") is node_graph.graph_selector_cardinality
    assert binding_registry.selector_cardinality("unknown") is None and binding_registry.selector_cardinality(None) is None


def test_fetch_selected_keeps_selection_count_semantics_when_a_download_fails(fake_upstreams):
    # The route that takes bytes counts the files selected, not the files delivered: a file the engine could not give is
    # left out as it always was, and a count of two is not refused afterwards. A delivered-count recheck would change this.
    from tests.test_node_graph_adapter import CONN, HOST

    def view(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("filename") == "gone.png":
            return httpx.Response(404)
        return httpx.Response(200, content=b"bytes")

    fake_upstreams.router.add_transport(HOST, HandlerTransport(view))
    history = job(n9={"images": [file("a.png"), file("gone.png")]})
    status = SimpleNamespace(record=node_graph.FinishedRecord(history, 10))
    delivered = asyncio.run(ADAPTER.fetch_selected(CONN, status, selecting("all"), counted(min=2, max=2), limits=LIMITS))
    assert delivered == {"result": [b"bytes"]}
