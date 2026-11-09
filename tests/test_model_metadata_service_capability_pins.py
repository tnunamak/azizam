"""Literal pins for service-capability precedence before its table moves to data."""

from dataclasses import asdict

import pytest

from gateway import model_metadata as mm


def _input_orders(*values):
    """Exercise input order and duplication, without consulting production tables."""
    forward = list(values)
    backward = list(reversed(values))
    return pytest.mark.parametrize(
        "capabilities",
        [forward, backward, forward * 2, backward * 2],
        ids=["forward", "reverse", "forward-duplicates", "reverse-duplicates"],
    )


def _fields(metadata):
    # Include None max_tokens and empty tags; to_fields() deliberately omits them.
    return None if metadata is None else asdict(metadata)


@_input_orders("chat", "completions")
def test_chat_and_completions_share_language_metadata(capabilities):
    assert _fields(mm.from_service_capabilities(capabilities)) == {
        "type": "language",
        "input_modalities": ("text",),
        "output_modalities": ("text",),
        "max_tokens": None,
        "tags": (),
    }


@_input_orders("chat", "embeddings")
def test_chat_wins_over_embeddings_regardless_of_input_order(capabilities):
    assert _fields(mm.from_service_capabilities(capabilities)) == {
        "type": "language",
        "input_modalities": ("text",),
        "output_modalities": ("text",),
        "max_tokens": None,
        "tags": (),
    }


@_input_orders("completions", "embeddings")
def test_completions_win_over_embeddings_regardless_of_input_order(capabilities):
    assert _fields(mm.from_service_capabilities(capabilities)) == {
        "type": "language",
        "input_modalities": ("text",),
        "output_modalities": ("text",),
        "max_tokens": None,
        "tags": (),
    }


@_input_orders("completions", "rerank")
def test_completions_win_over_rerank_regardless_of_input_order(capabilities):
    assert _fields(mm.from_service_capabilities(capabilities)) == {
        "type": "language",
        "input_modalities": ("text",),
        "output_modalities": ("text",),
        "max_tokens": None,
        "tags": (),
    }


@_input_orders("completions", "decisions")
def test_completions_win_over_decisions_regardless_of_input_order(capabilities):
    assert _fields(mm.from_service_capabilities(capabilities)) == {
        "type": "language",
        "input_modalities": ("text",),
        "output_modalities": ("text",),
        "max_tokens": None,
        "tags": (),
    }


@_input_orders("embeddings", "rerank")
def test_embeddings_win_over_rerank_regardless_of_input_order(capabilities):
    assert _fields(mm.from_service_capabilities(capabilities)) == {
        "type": "embedding",
        "input_modalities": ("text",),
        "output_modalities": ("embedding",),
        "max_tokens": None,
        "tags": (),
    }


@_input_orders("embeddings", "decisions")
def test_embeddings_win_over_decisions_regardless_of_input_order(capabilities):
    assert _fields(mm.from_service_capabilities(capabilities)) == {
        "type": "embedding",
        "input_modalities": ("text",),
        "output_modalities": ("embedding",),
        "max_tokens": None,
        "tags": (),
    }


@_input_orders("rerank", "decisions")
def test_rerank_wins_over_decisions_regardless_of_input_order(capabilities):
    assert _fields(mm.from_service_capabilities(capabilities)) == {
        "type": "reranking",
        "input_modalities": ("text",),
        "output_modalities": ("text",),
        "max_tokens": None,
        "tags": (),
    }


@_input_orders("vision", "rerank")
def test_vision_does_not_add_image_input_or_tags_to_rerank(capabilities):
    assert _fields(mm.from_service_capabilities(capabilities)) == {
        "type": "reranking",
        "input_modalities": ("text",),
        "output_modalities": ("text",),
        "max_tokens": None,
        "tags": (),
    }


@_input_orders("vision", "decisions")
def test_vision_does_not_add_image_input_or_tags_to_decisions(capabilities):
    assert _fields(mm.from_service_capabilities(capabilities)) == {
        "type": "decision",
        "input_modalities": ("text",),
        "output_modalities": ("text",),
        "max_tokens": None,
        "tags": (),
    }


@_input_orders("frobnicate", "chat")
def test_unknown_capability_does_not_hide_chat(capabilities):
    assert _fields(mm.from_service_capabilities(capabilities)) == {
        "type": "language",
        "input_modalities": ("text",),
        "output_modalities": ("text",),
        "max_tokens": None,
        "tags": (),
    }


@_input_orders(" chat ", "CHAT", None)
def test_chat_is_trimmed_lowercased_and_non_strings_are_ignored(capabilities):
    expected = {
        "type": "language",
        "input_modalities": ("text",),
        "output_modalities": ("text",),
        "max_tokens": None,
        "tags": (),
    }
    assert _fields(mm.from_service_capabilities(capabilities)) == expected
    # The mixed list otherwise masks losing either normalization step alone.
    assert _fields(mm.from_service_capabilities([" chat "])) == expected
    assert _fields(mm.from_service_capabilities(["CHAT"])) == expected


@_input_orders("llm.execution")
def test_llm_execution_is_not_a_service_capability_alias(capabilities):
    assert mm.from_service_capabilities(capabilities) is None


@_input_orders("openai-compatible")
def test_openai_compatible_is_not_a_service_capability_alias(capabilities):
    assert mm.from_service_capabilities(capabilities) is None


@_input_orders("embeddings", "rerank")
@pytest.mark.parametrize("current_loaded", [False, True], ids=["not-current", "current"])
def test_base_conflicting_service_capabilities_precede_current_loaded_without_origin(
    capabilities, current_loaded
):
    entry = {"id": "mixed-service", "azizam_current_loaded": current_loaded}
    sources = mm.MetadataSources(
        service_capabilities={"mixed-service": capabilities}.get,
    )

    assert _fields(mm.base_metadata(entry, sources)) == {
        "type": "embedding",
        "input_modalities": ("text",),
        "output_modalities": ("embedding",),
        "max_tokens": None,
        "tags": (),
    }


@_input_orders("embeddings", "rerank")
@pytest.mark.parametrize("current_loaded", [False, True], ids=["not-current", "current"])
def test_base_conflicting_service_capabilities_precede_llm_origin_and_current_loaded(
    capabilities, current_loaded
):
    entry = {"id": "mixed-service", "azizam_current_loaded": current_loaded}
    sources = mm.MetadataSources(
        service_capabilities={"mixed-service": capabilities}.get,
        origins={"mixed-service": "llm-catalog"},
    )

    assert _fields(mm.base_metadata(entry, sources)) == {
        "type": "embedding",
        "input_modalities": ("text",),
        "output_modalities": ("embedding",),
        "max_tokens": None,
        "tags": (),
    }
