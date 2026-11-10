"""Registry-driven capability identities and presentation must not grant access."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from typing import Literal, get_args

import pytest
from pydantic import ValidationError
import yaml

from gateway import access_preview, capability_ids, owner_projection
from gateway.capabilities import Capability
from gateway.policy import DecidingRule


LEGACY_ORDER = (
    "chat", "image", "video", "mesh", "music", "sfx", "speech", "audio",
    "transcribe", "decide", "embed", "rerank",
)
LEGACY_IDS = (
    "chat", "image", "video", "speech", "transcribe", "music", "sfx", "audio",
    "decide", "embed", "rerank", "mesh",
)


def _document():
    return yaml.safe_load(capability_ids.CAPABILITY_WORDS_PATH.read_text(encoding="utf-8"))


def _write(tmp_path, document):
    path = tmp_path / "capability_words.yml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _fresh(monkeypatch, original, suffix):
    name = f"gateway._capability_presentation_{suffix}"
    spec = importlib.util.spec_from_file_location(name, original.__file__)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _consumers(monkeypatch, document):
    # Exercise the real import-time reader without reloading shared application modules.
    original_read = Path.read_text
    serialized = yaml.safe_dump(document, sort_keys=False)

    def read(path, *args, **kwargs):
        if path == capability_ids.CAPABILITY_WORDS_PATH:
            return serialized
        return original_read(path, *args, **kwargs)

    with monkeypatch.context() as data_patch:
        data_patch.setattr(Path, "read_text", read)
        ids = _fresh(monkeypatch, capability_ids, "ids")
    monkeypatch.setitem(sys.modules, "gateway.capability_ids", ids)
    projection = _fresh(monkeypatch, owner_projection, "projection")
    preview = _fresh(monkeypatch, access_preview, "preview")
    return ids, projection, preview


def _row(name, capability, label, intent):
    return {
        "id": name, "display_name": name.title(), "alias": False,
        "capability": capability, "capability_title": label,
        "capabilities": [{"capability": capability, "label": label, "intent": intent}],
    }


def _catalog(*rows):
    return {"aliases": [row for row in rows if row["alias"]],
            "models": [row for row in rows if not row["alias"]]}


def test_unfamiliar_capability_projects_and_orders_from_registry(monkeypatch):
    document = _document()
    for index, row in enumerate(document["capabilities"], start=1):
        row["display_order"] = index
    document["capabilities"].append({
        "id": "fixture_weave", "label": "Weaving", "display_order": 0,
    })
    document["route_intents"]["fixture.weave"] = "fixture_weave"
    ids, projection, preview = _consumers(monkeypatch, document)

    assert ids.CAPABILITY_DISPLAY_ORDER["fixture_weave"] == 0
    for kind in ("receipt", "job"):
        shown = projection.project(kind, {"intent": "fixture.weave"}).model_dump(mode="json")
        assert shown["capability"] == "fixture_weave"
    assert projection.ReceiptView(capability="fixture_weave").capability == "fixture_weave"
    assert projection.JobView(capability="fixture_weave").capability == "fixture_weave"

    rows = [_row(name, "fixture_weave", "Weaving", "fixture.weave")
            for name in ("open", "shut", "uncertain", "permitted-only")]
    known = _row("known", "chat", "Chat", "chat.completions")
    # An undeclared catalog row must not be filtered by a presentation registry.
    unlisted = _row("unlisted", "fixture_unlisted", "Unlisted", "fixture.unlisted")
    unknown_rule = DecidingRule(
        code="category.rules_unavailable", kind="category", id=None, side=None,
    )
    denied = [
        Capability(intent="fixture.weave", backend="svc", model="shut",
                   policy_available=False, policy_reasons=("Recorded refusal.",)),
        Capability(intent="fixture.weave", backend="svc", model="uncertain",
                   policy_available=False, policy_rule=unknown_rule),
    ]
    kwargs = {
        "offered": _catalog(known, *rows[:3], unlisted),
        "permitted": _catalog(known, rows[0], rows[3], unlisted),
        "denied": denied,
    }
    original = deepcopy(kwargs)
    result = preview.build_access_preview(**kwargs)
    assert kwargs == original
    assert [group["group"] for group in result["groups"]] == [
        "fixture_weave", "chat", "fixture_unlisted",
    ]
    assert {entry["id"]: entry["state"] for entry in result["entries"]} == {
        "open": "allowed", "shut": "blocked", "uncertain": "unknown",
        "permitted-only": "allowed", "known": "allowed", "unlisted": "allowed",
    }
    assert result["summary"] == {"usable": 4, "total": 6, "blocked": 1, "unknown": 1}
    by_id = {entry["id"]: entry for entry in result["entries"]}
    assert by_id["shut"]["reason"] == "Recorded refusal."
    assert by_id["uncertain"]["rule"]["code"] == unknown_rule.code

    # Rank changes may reorder entries, but cannot change entries or decisions.
    document["capabilities"][-1]["display_order"] = 100
    _, _, reordered_preview = _consumers(monkeypatch, document)
    reordered = reordered_preview.build_access_preview(**kwargs)
    assert [group["group"] for group in reordered["groups"]] == [
        "chat", "fixture_weave", "fixture_unlisted",
    ]
    assert reordered["summary"] == result["summary"]
    assert {entry["id"]: entry for entry in reordered["entries"]} == by_id


def test_undeclared_capability_is_rejected_or_omitted(monkeypatch):
    _, projection, _ = _consumers(monkeypatch, _document())
    for schema in (projection.ReceiptView, projection.JobView):
        with pytest.raises(ValidationError):
            schema(capability="fixture_undeclared")
    for kind in ("receipt", "job"):
        assert projection.project(kind, {"intent": "fixture.undeclared"}).capability is None
    # Even a stale injected intent mapping cannot escape the closed vocabulary.
    monkeypatch.setattr(projection, "INTENT_CAPABILITIES", {"fixture.stale": "fixture_undeclared"})
    assert projection.project("receipt", {"intent": "fixture.stale"}).capability is None


@pytest.mark.parametrize("value", [True, -1, 1.5, "0", None])
def test_display_order_rejects_invalid_ranks(tmp_path, value):
    document = _document()
    document["capabilities"][0]["display_order"] = value
    with pytest.raises(ValueError, match="display_order.*non-negative integer"):
        capability_ids._load_capability_data(_write(tmp_path, document))


def test_display_order_rejects_duplicates(tmp_path):
    document = _document()
    for row in document["capabilities"][:2]:
        row["display_order"] = 42
    with pytest.raises(ValueError, match="duplicate display_order"):
        capability_ids._load_capability_data(_write(tmp_path, document))


def test_capability_rows_still_refuse_unknown_fields_and_references(tmp_path):
    for mutate, message in (
        (lambda doc: doc["capabilities"][0].update(python_import="example.module"),
         "unknown key.*python_import"),
        (lambda doc: doc["route_intents"].update({"fixture.unknown": "undeclared"}),
         "unknown capability"),
    ):
        document = _document()
        mutate(document)
        with pytest.raises(ValueError, match=message):
            capability_ids._load_capability_data(_write(tmp_path, document))


def test_optional_ranks_preserve_existing_loader_shapes_and_file_order(tmp_path):
    document = _document()
    for row in document["capabilities"]:
        row.pop("display_order", None)
    path = _write(tmp_path, document)
    ranks = capability_ids.load_capability_display_order(path)
    assert tuple(ranks) == tuple(row["id"] for row in document["capabilities"])
    assert tuple(ranks.values()) == tuple(range(len(ranks)))
    with pytest.raises(TypeError):
        ranks["fixture_new"] = 0
    assert len(capability_ids._load_capability_data(path)) == 4
    assert len(capability_ids.load_capability_presentation(path)) == 3
    assert len(capability_ids.load_capability_classification(path)) == 3
    assert len(capability_ids.load_capability_words(path)) == 2


def test_shipped_projection_and_preview_outputs_match_legacy_vocabulary(monkeypatch):
    assert get_args(owner_projection.Capability) == LEGACY_IDS
    assert access_preview.GROUP_ORDER == LEGACY_ORDER
    for intent, capability in capability_ids.INTENT_CAPABILITIES.items():
        for kind in ("receipt", "job"):
            shown = owner_projection.project(kind, {"intent": intent})
            # Full output must equal the old fixed-vocabulary path, not just its field.
            with monkeypatch.context() as legacy:
                legacy.setattr(owner_projection, "Capability",
                               Literal[LEGACY_IDS])
                before = owner_projection.project(kind, {"intent": intent})
            assert shown.model_dump(mode="json") == before.model_dump(mode="json")
            assert shown.capability == capability

    rows = [_row(capability, capability, capability_ids.CAPABILITY_LABELS[capability],
                 next(intent for intent, value in capability_ids.INTENT_CAPABILITIES.items()
                      if value == capability))
            for capability in reversed(LEGACY_ORDER)]
    # Cover alias-before-model and case-insensitive name tie breakers too.
    rows.extend([{**rows[0], "id": "z-alias", "alias": True},
                 {**rows[0], "id": "a-model", "display_name": "A model"}])
    offered = _catalog(*rows)
    offered["aliases"] = [row for row in rows if row["alias"]]
    offered["models"] = [row for row in rows if not row["alias"]]
    permitted = _catalog(*rows[::2])
    kwargs = dict(offered=offered, permitted=permitted, denied=[])
    shown = access_preview.build_access_preview(**kwargs)
    with monkeypatch.context() as legacy:
        legacy.setattr(access_preview, "GROUP_ORDER", LEGACY_ORDER)
        before = access_preview.build_access_preview(**kwargs)
    assert shown == before
    assert [group["group"] for group in shown["groups"]] == list(LEGACY_ORDER)
