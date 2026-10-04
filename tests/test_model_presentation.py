"""Catalog presentation must preserve placement evidence and hide registry IDs."""

import pytest

from gateway.capabilities import Capability
from gateway.models_catalog import build_models_catalog
from gateway.model_presentation import engine_label, model_locality, service_health_endpoint


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:18001", "http://[::1]:18001", "http://localhost:18001"])
def test_direct_loopback_engine_is_local(endpoint):
    catalog = build_models_catalog(
        capabilities=[Capability(intent="chat.completions", backend="capture-engine", model="demo")],
        service_records={"capture-engine": {"name": "Local test engine", "config": {"base_url": endpoint}}},
    )
    row = catalog["models"][0]
    assert row["engine_label"] == "Local test engine"
    assert row["locality"] == "local"


def test_explicit_remote_model_wins_over_local_routing_address():
    assert model_locality({"config": {"base_url": "http://localhost:18001"}}, {"locality": "remote"}) == "remote"
    assert model_locality({"config": {"base_url": "http://localhost:18001", "remote_provider": True}}, None) == "remote"


@pytest.mark.parametrize("service", [
    {"config": {"base_url": "http://10.0.0.3:18001"}},
    {"config": {"base_url": "https://example.test"}},
    {"config": {"host": "engine-container"}},
    {"kind": "llm-substrate", "config": {"base_url": "http://localhost:18001"}},
    {"kind": "litellm", "config": {"base_url": "http://localhost:18001"}},
    {"id": "litellm", "config": {"base_url": "http://localhost:18001"}},
    {"config": {"execution_substrate": "router", "base_url": "http://localhost:18001"}},
])
def test_unproven_placement_stays_unknown(service):
    assert model_locality(service, None) == "unknown"


def test_engine_label_never_falls_back_to_registry_id():
    assert engine_label({"name": "capture-engine"}, "capture-engine") is None
    assert engine_label({"name": "capture-engine", "config": {"display_name": "Chat engine"}}, "capture-engine") == "Chat engine"


def test_catalog_preserves_keyed_substrate_identity_without_claiming_local_cost():
    catalog = build_models_catalog(
        capabilities=[Capability(intent="chat.completions", backend="litellm", model="demo")],
        service_records={"litellm": {"config": {"base_url": "http://localhost:18001"}}},
    )
    row = catalog["models"][0]
    assert row["locality"] == "unknown"
    assert row["cost"]["kind"] == "unknown"


@pytest.mark.parametrize("connection, expected", [
    ({"host": "127.0.0.1", "port": 18001, "base_path": "/v1", "health_endpoint": "/health"}, "http://127.0.0.1:18001/health"),
    ({"host": "::1", "port": 18001, "base_path": "/v1", "health_endpoint": "health"}, "http://[::1]:18001/v1/health"),
    ({"host": "example.test", "scheme": "https", "health_endpoint": "/health"}, "https://example.test/health"),
    ({"base_url": "http://example.test/v1", "health_endpoint": "health"}, "http://example.test/v1/health"),
    ({"health_endpoint": "http://example.test/health"}, "http://example.test/health"),
    ({"host": "example.test"}, None),
    ({"health_endpoint": "/health"}, None),
])
def test_declared_health_endpoint_resolves_both_registry_connection_shapes(connection, expected):
    assert service_health_endpoint({"config": connection}) == expected


def test_normalized_service_health_is_probed_instead_of_being_reported_down():
    import asyncio
    from gateway.models_catalog import probe_service_health
    from gateway.service_registry import service_form_to_raw

    raw = service_form_to_raw({"name": "Chat engine", "base_url": "http://127.0.0.1:18001/v1", "health_endpoint": "/health"})
    assert "base_url" not in raw

    async def get_json(url):
        assert url == "http://127.0.0.1:18001/health"
        return {"status": "ok"}

    evidence = asyncio.run(probe_service_health({"chat": {"config": raw}}, get_json=get_json))
    assert evidence["chat"]["available"] is True
    assert evidence["chat"]["status"] == "ready"


def test_declared_taxonomy_and_friendly_title_do_not_guess_engine_names():
    from gateway.model_presentation import capability_presentation
    cap = Capability(intent="audio.generate", backend="test", workflow="internal.workflow",
                     title="Configured instrument", capability="music")
    presentation = capability_presentation(cap)
    assert presentation["title"] == "Configured instrument"
    assert presentation["capability_title"] == "Music"
    assert presentation["capability"] == "music"
    assert presentation["icon_key"] == "music"
    assert presentation["use_names"] == ["internal.workflow"]
    unspecified = Capability(intent="audio.generate", backend="test", model="music-looking-name")
    assert capability_presentation(unspecified)["capability"] == "audio"
    assert capability_presentation(unspecified)["capability_title"] == "Audio"
    assert capability_presentation(unspecified)["icon_key"] == "sfx"


def test_alias_hides_only_its_same_backend_and_intent_target():
    from gateway.capabilities import caller_visible_capabilities
    alias = Capability(intent="audio.generate", backend="test", model="music", tags=("alias", "resolves_to:actual"),
                       resolved_workflow="internal.workflow")
    concrete = Capability(intent="audio.generate", backend="test", model="actual", workflow="internal.workflow")
    different_backend = Capability(intent="audio.generate", backend="other", model="actual")
    different_intent = Capability(intent="speech.synthesis", backend="test", model="actual")
    assert set(caller_visible_capabilities([alias, concrete, different_backend, different_intent])) == {
        alias, different_backend, different_intent,
    }
    denied = Capability(intent="audio.generate", backend="test", model="music", tags=alias.tags, policy_available=False)
    assert concrete in caller_visible_capabilities([denied, concrete])


def test_catalog_summary_omits_unknown_metrics_but_preserves_details():
    catalog = build_models_catalog(
        capabilities=[Capability(intent="chat.completions", backend="test", model="demo")],
        service_records={"test": {"name": "Internal service", "description": "Private path debug data", "config": {"base_url": "http://localhost:18001"}}},
    )
    row = catalog["models"][0]
    assert row["summary"] == {"cost_label": "Local · no provider charge"}
    assert row["timing"] == {"typical_seconds": None, "source": None}
    assert row["description"] == "A separate chat profile named demo."


def test_workflow_alias_catalog_is_one_caller_entry_with_metadata():
    catalog = build_models_catalog(
        capabilities=[Capability(intent="audio.generate", backend="test", workflow="internal.workflow")],
        alias_views={"audio_generate": {"stable": [{"alias": "music", "resolves_to": "internal.workflow", "display_name": "My music"}]}},
        workflow_records=[{"id": "internal.workflow", "display_name": "Instrument", "capability": "music"}],
    )
    assert catalog["models"] == []
    row = catalog["aliases"][0]
    assert row["display_name"] == "My music"
    assert row["capabilities"][0]["capability"] == "music"
    assert row["resolves_to"] == {"audio_generate": "internal.workflow"}


def test_declared_workflow_metadata_survives_alias_target_resolution():
    from gateway.capabilities import resolve_capability_targets, summarize_capability
    target = Capability(intent="audio.generate", backend="test", workflow="actual",
                        title="Instrument", capability="music")
    alias = Capability(intent="audio.generate", backend="test", model="music", tags=("alias", "resolves_to:actual"))
    projected = resolve_capability_targets([alias, target])
    summary = summarize_capability(next(cap for cap in projected if cap.model == "music"), "client")
    assert summary["title"] == "Instrument"
    assert summary["capability"] == "music"
    assert summary["use_name"] == "music"


def test_technical_discovery_retains_workflow_targets_while_product_rows_dedupe():
    from gateway.capabilities import caller_visible_capabilities, summarize_capabilities
    target = Capability(intent="audio.generate", backend="test", workflow="actual")
    alias = Capability(intent="audio.generate", backend="test", model="music",
                       tags=("alias", "resolves_to:actual"))
    discovery = summarize_capabilities([target, alias], "client")["capabilities"]
    assert len(discovery) == 2
    assert any(row.get("workflow") == "actual" for row in discovery)
    assert caller_visible_capabilities([target, alias]) == [alias]


@pytest.mark.parametrize("filename, taxonomy", [
    ("ace-step-1.5-xl-turbo.yml", "music"), ("ace-step-1.5-2b-base.yml", "music"),
    ("yue2-song.yml", "music"), ("sa3-small-sfx.yml", "sfx"), ("tangoflux.yml", "audio"),
])
def test_shipped_audio_taxonomy_is_data_in_valid_manifests(filename, taxonomy):
    from pathlib import Path
    from gateway.manifests import load_workflow_manifest
    from gateway.model_presentation import product_capability
    manifest = load_workflow_manifest(Path(__file__).resolve().parents[1] / "manifests/workflows" / filename)
    assert product_capability("audio.generate", manifest) == taxonomy


def test_chat_alias_does_not_hide_embeddings_of_same_model():
    catalog = build_models_catalog(
        alias_views={"chat": {"stable": [{"alias": "chat", "resolves_to": "multi"}]}},
        capabilities=[Capability(intent="chat.completions", backend="one", model="multi"),
                      Capability(intent="text.embeddings", backend="one", model="multi")],
    )
    assert catalog["aliases"][0]["id"] == "chat"
    assert catalog["models"][0]["id"] == "multi"
    assert {row["intent"] for row in catalog["models"][0]["capabilities"]} == {
        "chat.completions", "text.embeddings",
    }


def test_alias_does_not_hide_same_model_on_another_backend():
    catalog = build_models_catalog(
        alias_views={"chat": {"stable": [{"alias": "chat", "resolves_to": "multi", "service": "one"}]}},
        capabilities=[Capability(intent="chat.completions", backend="one", model="multi"),
                      Capability(intent="chat.completions", backend="two", model="multi")],
    )
    assert catalog["models"][0]["id"] == "multi"


@pytest.mark.parametrize("name, capabilities, engine, expected", [
    ("Audio & Speech (provider)", ["audio", "speech"], "Chatterbox", "Chatterbox (speech)"),
    ("Image (Provider)", ["images", "videos"], "ComfyUI", "ComfyUI (images & video)"),
    ("ComfyUI (Image + Video Generation)", ["images", "videos"], None, "ComfyUI (images & video)"),
    ("Canary", ["transcriptions"], None, "Canary (transcription)"),
    ("llama.cpp", ["chat", "completions"], None, "llama.cpp (chat)"),
])
def test_local_engine_labels_come_from_declared_capabilities(name, capabilities, engine, expected):
    service = {"name": name, "capabilities": capabilities,
               "config": {"locality": "local", "engine_name": engine}}
    assert engine_label(service, "internal") == expected


def test_public_workflow_ids_and_manifest_ids_share_one_friendly_alias_entry():
    from gateway.capabilities import canonicalize_workflow_capabilities, caller_visible_capabilities, resolve_capability_targets, summarize_capability
    raw = Capability(intent="audio.generate", backend="test", workflow="instrument")
    manifest = Capability(intent="audio.generate", backend="test", workflow="audio.generate.instrument",
                          title="Configured instrument", capability="music")
    alias = Capability(intent="audio.generate", backend="gateway", model="music", tags=("alias", "resolves_to:instrument"))
    entries = [{"id": "instrument", "vivid_fish_workflow_id": "audio.generate.instrument",
                "vivid_fish_intent": "audio.generate", "vivid_fish_backend": "test"}]
    canonical = canonicalize_workflow_capabilities([raw, manifest, alias], entries)
    visible = caller_visible_capabilities(resolve_capability_targets(canonical))
    assert len(visible) == 1
    row = summarize_capability(visible[0], "client")
    assert row["title"] == "Configured instrument"
    assert row["use_name"] == "music"
    assert visible[0].resolved_workflow == "audio.generate.instrument"


def test_workflow_canonicalization_keeps_distinct_public_names_and_shapes():
    from gateway.capabilities import canonicalize_workflow_capabilities, caller_visible_capabilities
    first = Capability(intent="image.generate", backend="one", workflow="first")
    second = Capability(intent="image.generate", backend="one", workflow="second")
    other = Capability(intent="image.generate", backend="two", workflow="first")
    other_intent = Capability(intent="image.edit", backend="one", workflow="first")
    entries = [{"id": name, "vivid_fish_workflow_id": "image." + name,
                "vivid_fish_intent": "image.generate", "vivid_fish_backend": "one"}
               for name in ("first", "second")]
    rows = canonicalize_workflow_capabilities([first, second, other, other_intent], entries)
    assert [(cap.model, cap.workflow) for cap in rows[:2]] == [("first", "image.first"), ("second", "image.second")]
    assert rows[2:] == [other, other_intent]
    assert len(caller_visible_capabilities(rows)) == 4


def test_ambiguous_workflow_mapping_does_not_invent_a_target():
    from gateway.capabilities import canonicalize_workflow_capabilities
    raw = Capability(intent="audio.generate", backend="test", workflow="public")
    entries = [{"id": "public", "vivid_fish_workflow_id": workflow,
                "vivid_fish_intent": "audio.generate", "vivid_fish_backend": "test"}
               for workflow in ("audio.one", "audio.two")]
    assert canonicalize_workflow_capabilities([raw], entries) == [raw]


def test_role_target_alias_has_one_caller_entry_without_hiding_other_shapes():
    from gateway.capabilities import caller_visible_capabilities, resolve_capability_targets, summarize_capability
    role = Capability(intent="chat.completions", backend="one", model="role:coder", title="Coding profile")
    alias = Capability(intent="chat.completions", backend="gateway", model="coder", tags=("alias", "resolves_to:coder"))
    other_backend = Capability(intent="chat.completions", backend="two", model="role:coder")
    other_intent = Capability(intent="text.embeddings", backend="one", model="role:coder")
    # Explicit alias backend proves which of the identically named roles serves it.
    alias = Capability(intent=alias.intent, backend="one", model=alias.model, tags=alias.tags)
    rows = caller_visible_capabilities(resolve_capability_targets([role, alias, other_backend, other_intent]))
    assert len(rows) == 3
    assert role not in rows
    assert other_backend in rows and other_intent in rows
    caller = summarize_capability(next(cap for cap in rows if cap.model == "coder"), "client")
    assert caller["use_name"] == "coder"
    assert caller["title"] == "Coder"


def test_unrelated_role_ids_and_unresolved_aliases_remain_visible():
    from gateway.capabilities import caller_visible_capabilities
    role = Capability(intent="chat.completions", backend="one", model="role:other")
    alias = Capability(intent="chat.completions", backend="one", model="coder", tags=("alias", "resolves_to:coder"))
    unresolved_role = Capability(intent="chat.completions", backend="two", model="role:coder")
    assert len(caller_visible_capabilities([role, alias, unresolved_role])) == 3


def test_catalog_logical_role_alias_dedupes_only_covered_chat_role():
    catalog = build_models_catalog(
        alias_views={"chat": {"stable": [{"alias": "coder", "resolves_to": "coder", "service": "one"}]}},
        capabilities=[Capability(intent="chat.completions", backend="one", model="role:coder", title="Coding profile"),
                      Capability(intent="chat.completions", backend="one", model="coder", tags=("alias", "resolves_to:coder"))],
    )
    assert catalog["models"] == []
    assert catalog["aliases"][0]["id"] == "coder"
    assert catalog["aliases"][0]["display_name"] == "Coder"


def test_alias_resolution_carries_target_limits_and_preserves_alias_override():
    from gateway.capabilities import resolve_capability_targets
    target = Capability(intent="video.generate", backend="one", workflow="canonical",
                        limits=(("max_total_seconds", 120), ("segment_seconds", 10)))
    alias = Capability(intent="video.generate", backend="one", model="video", tags=("alias", "resolves_to:canonical"))
    resolved = next(cap for cap in resolve_capability_targets([alias, target]) if cap.model == "video")
    assert dict(resolved.limits) == {"max_total_seconds": 120, "segment_seconds": 10}
    override = Capability(intent=alias.intent, backend=alias.backend, model=alias.model,
                          tags=alias.tags, limits=(("max_total_seconds", 60),))
    assert next(cap for cap in resolve_capability_targets([override, target]) if cap.model == "video").limits == override.limits


def test_permitted_registered_public_role_hides_internal_spelling_only_same_shape():
    from gateway.capabilities import caller_visible_capabilities, summarize_capability
    public = Capability(intent="chat.completions", backend="one", model="coder-q5", title="Coding profile")
    internal = Capability(intent="chat.completions", backend="one", model="role:coder-q5")
    other_backend = Capability(intent="chat.completions", backend="two", model="role:coder-q5")
    other_intent = Capability(intent="text.embeddings", backend="one", model="role:coder-q5")
    rows = caller_visible_capabilities([public, internal, other_backend, other_intent])
    assert rows == sorted([public, other_backend, other_intent], key=lambda cap: cap.sort_key)
    assert summarize_capability(public, "client")["title"] == "Coder Q5"
    denied = Capability(intent=public.intent, backend=public.backend, model=public.model, policy_available=False)
    assert internal in caller_visible_capabilities([denied, internal])


def test_catalog_direct_public_role_pair_hides_only_fully_covered_internal_row():
    public = Capability(intent="chat.completions", backend="one", model="coder-q5", title="Coding profile")
    internal = Capability(intent="chat.completions", backend="one", model="role:coder-q5")
    catalog = build_models_catalog(capabilities=[public, internal])
    assert [row["id"] for row in catalog["models"]] == ["coder-q5"]
    assert catalog["models"][0]["display_name"] == "Coder Q5"
    extra = Capability(intent="text.embeddings", backend="one", model="role:coder-q5")
    catalog = build_models_catalog(capabilities=[public, internal, extra])
    assert {row["id"] for row in catalog["models"]} == {"coder-q5", "role:coder-q5"}
    role = next(row for row in catalog["models"] if row["id"] == "role:coder-q5")
    assert {cap["intent"] for cap in role["capabilities"]} == {"chat.completions", "text.embeddings"}
    other_backend = Capability(intent="chat.completions", backend="two", model="role:coder-q5")
    assert len(build_models_catalog(capabilities=[public, other_backend])["models"]) == 2
    denied = Capability(intent=public.intent, backend=public.backend, model=public.model, policy_available=False)
    assert [row["id"] for row in build_models_catalog(capabilities=[denied, internal])["models"]] == ["role:coder-q5"]


def test_technical_summary_dedupes_exact_entries_and_keeps_alias_target_details():
    from gateway.capabilities import caller_visible_capabilities, summarize_capabilities
    alias = Capability(intent="chat.completions", backend="one", model="fast", tags=("alias", "resolves_to:actual"))
    unknown = Capability(intent="chat.completions", backend="one", model="actual")
    ready = Capability(intent="chat.completions", backend="two", model="actual",
                       availability={"status": "ready", "available": True})
    rows = summarize_capabilities([alias, unknown, ready], "client")["capabilities"]
    assert {row["model"] for row in rows} == {"fast", "actual"}
    assert len(rows) == 2
    assert next(row for row in rows if row["model"] == "actual")["availability"]["status"] == "ready"
    # Product suppression stays backend-scoped even though exact identity
    # preference chooses the best observed target from the remaining entries.
    assert {cap.model for cap in caller_visible_capabilities([alias, unknown, ready])} == {"fast", "actual"}


@pytest.mark.parametrize("kind", ["chat", "image", "video", "music", "sfx", "speech", "transcribe", "decide", "embed", "rerank"])
def test_presentation_taxonomy_has_distinct_icon_keys(kind):
    from gateway.model_presentation import capability_presentation
    row = capability_presentation(Capability(intent="audio.generate", backend="one", capability=kind))
    assert row["icon_key"] == kind
    assert row["capability"] == kind
    assert row["summary"].endswith(".")


def test_media_synonyms_and_target_share_one_catalog_card():
    catalog = build_models_catalog(
        capabilities=[Capability(intent="audio.generate", backend="one", model="instrument", title="Configured instrument", capability="music")],
        alias_views={"audio_generate": {"stable": [
            {"alias": "music", "resolves_to": "instrument"},
            {"alias": "song", "resolves_to": "instrument"},
        ]}},
    )
    assert catalog["models"] == []
    assert len(catalog["aliases"]) == 1
    row = catalog["aliases"][0]
    assert set(row["use_names"]) == {"music", "song", "instrument"}
    assert row["icon_key"] == "music"
    assert row["recommended_model"] == "Configured instrument"


def test_distinct_chat_profiles_keep_alias_titles_and_metadata_summary():
    catalog = build_models_catalog(
        capabilities=[Capability(intent="chat.completions", backend="one", model="actual", title="Declared model")],
        alias_views={"chat": {"stable": [
            {"alias": "default", "resolves_to": "actual", "description": "Thinks before it answers."},
            {"alias": "fast", "resolves_to": "actual", "description": "Answers without thinking."},
            {"alias": "coder-q5", "resolves_to": "actual", "description": "Coding with the configured Q5 model."},
        ]}},
    )
    rows = {row["id"]: row for row in catalog["aliases"]}
    assert rows["default"]["title"] == "Default"
    assert rows["fast"]["title"] == "Fast"
    assert rows["default"]["recommended_model"] == "Declared model"
    assert rows["coder-q5"]["description"] == "Coding with the configured Q5 model."
    assert rows["coder-q5"]["secondary_profile"]
    assert not rows["default"]["secondary_profile"]


def test_product_status_recommends_ready_model_once_per_taxonomy():
    from gateway.model_presentation import capability_presentation, group_product_capabilities, product_status_summary
    rows = []
    for name, state in [("music", "unavailable"), ("song", "ready"), ("instrument", "ready")]:
        row = capability_presentation(Capability(intent="audio.generate", backend="one", model=name, capability="music"))
        rows.append({**row, "status": state, "available": state == "ready"})
    grouped = group_product_capabilities(rows)
    assert len(grouped) == 1
    assert grouped[0]["available"]
    assert set(grouped[0]["use_names"]) == {"music", "song", "instrument"}
    assert product_status_summary(grouped) == {"state": "ready", "reason": "Everything you can use is ready."}


def test_partial_summary_names_unavailable_capability_and_safe_reason():
    from gateway.model_presentation import product_status_summary
    rows = [{"capability": "chat", "capability_title": "Chat", "status": "ready", "available": True},
            {"capability": "image", "capability_title": "Images", "status": "unavailable", "available": False,
             "reason": "secret backend diagnostic", "availability": {"evidence": {"kind": "disabled"}}}]
    assert product_status_summary(rows) == {
        "state": "unavailable", "reason": "Images are unavailable: the service is disabled."}
    assert "secret" not in product_status_summary(rows)["reason"]
    assert product_status_summary([])["state"] == "unavailable"


def test_owner_transcription_uses_same_taxonomy_even_without_live_observation():
    from gateway.admin_api import derive_admin_health_projection
    health = derive_admin_health_projection(
        {"services": {"canary-stt": {"name": "Canary", "capabilities": ["transcriptions"],
                                   "description": "Internal deployment note"}}},
        {}, observation_available=False,
    )
    row = health["services"][0]
    assert row["engine_label"] == "Canary (transcription)"
    assert row["capability_labels"] == ["Transcription"]
    assert row["icon_key"] == "transcribe"
    assert "Internal" not in row["name"]


@pytest.mark.parametrize("name,capabilities,expected", [
    ("Local llama.cpp server (official; Bee rollback available)", ["openai", "chat", "completions"], "llama.cpp (chat)"),
    ("canary-stt", ["audio", "transcriptions"], "Canary (transcription)"),
    ("chatterbox-tts", ["audio", "speech"], "Chatterbox (speech)"),
])
def test_legacy_seed_names_keep_identity_and_drop_deployment_notes(name, capabilities, expected):
    assert engine_label({"name": name, "capabilities": capabilities}, "internal") == expected


def test_remote_concrete_model_id_preserves_provider_spelling_without_authored_title():
    catalog = build_models_catalog(
        capabilities=[Capability(intent="chat.completions", backend="cloud", model="gpt-4.1-nano")],
        service_records={"cloud": {"name": "Provider", "config": {"locality": "remote"}}},
    )
    assert catalog["models"][0]["title"] == "gpt-4.1-nano"
    assert catalog["models"][0]["display_name"] == "gpt-4.1-nano"


def test_alias_keeps_target_model_specific_description():
    catalog = build_models_catalog(
        capabilities=[Capability(intent="chat.completions", backend="one", model="actual")],
        alias_views={"chat": {"stable": [{"alias": "coder-q5", "resolves_to": "actual"}]}},
        service_records={"one": {"config": {"models": [{"id": "actual", "upstream_model": "Configured weights", "context_window": 4096}]}}},
    )
    assert catalog["aliases"][0]["description"] == "Uses Configured weights, supports a context of 4096 tokens."


def test_internal_role_names_are_hidden_only_with_permitted_public_counterpart():
    from gateway.model_presentation import capability_presentation, group_product_capabilities
    rows = [{**capability_presentation(Capability(intent="chat.completions", backend="one", model=name)),
             "status": "ready", "available": True} for name in ("role:default", "default", "role:other")]
    assert group_product_capabilities(rows)[0]["use_names"] == ["default", "role:other"]


def test_declared_role_weights_supply_recommendation_without_private_path():
    from gateway.local_capabilities import build_local_capabilities
    from gateway.capabilities import summarize_capability
    capabilities = build_local_capabilities(
        {"roles": {"default": {"backend": "one", "model": "/private/models/Declared-Weights-Q4.gguf"}}},
        backends={"one": {"capabilities": ["chat"]}},
    )
    row = summarize_capability(next(cap for cap in capabilities if cap.model == "role:default"), "client")
    assert row["title"] == "Default"
    assert row["recommended_model"] == "Declared-Weights-Q4"
    assert "/private" not in str(row)
