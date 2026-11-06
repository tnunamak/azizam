"""The route graph is frozen: frontends may move from decorators to the mount table, not change.

tests/fixtures/route_graph_frozen.json is the graph of origin/main before the frontends mounted
from registry/protocol_frontends.yml. Every frozen route, dispatch outcome, policy projection and
OpenAPI digest must still be there. A route may be added only where ADDED_PATHS names its path
and says why. Nothing may be removed, re-handled, re-methoded or re-policied.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import proxy
import route_graph

FIXTURE = Path(__file__).parent / "fixtures" / "route_graph_frozen.json"

# Deliberate additions since the freeze: path prefix -> reason.
ADDED_PATHS: dict[str, str] = {
    "/internal/litellm/local": "the gateway lifecycle hop serves completions and embeddings next to chat (one route per endpoint "
                               "in capability_ids.INNER_HOP_ENDPOINTS), and a POST no route serves answers 404 endpoint_not_served "
                               "before the key check; tests/test_litellm_inner_hop_endpoints.py covers them",
    "/auth/api/clear-browser-cache": "the sign-out page asks the browser to drop cached files without waiting; tests/test_session_asset_urls.py covers it",
    "/me/api/apps/{app_id}/trusted-addresses": "a person trusts an address for their own App; tests/test_me_trusted_addresses.py covers it",
    "/me/api/credentials/{credential_id}/adopt": "a person keeps an unproven key on their own App; tests/test_me_binding_adoption.py covers it",
    "/me/api/apps/{app_id}/trusted-addresses/{entry_id}/adopt": "a person keeps an unproven address on their own App; tests/test_me_binding_adoption.py covers it",
    "/me/api/apps/{app_id}/trusted-addresses/{entry_id}/readd": "a person adds an unkept address again on their own App in one step; tests/test_me_binding_adoption.py covers it",
    "/me/api/credentials/{credential_id}/replace": "a person replaces a key they cannot keep, on their own App; tests/test_me_binding_adoption.py covers it",
    "/me/api/apps/{app_id}/trusted-addresses/{entry_id}": "a person removes a trusted address from their own App; tests/test_me_binding_adoption.py covers it",
    "/v1/meshes/generate": "source-defined mesh route owned by the mesh frontend: one image in, one GLB out, fail-closed until capacity is enabled; tests/test_mesh_route.py and tests/test_mesh_manifest.py cover it",
    "/admin/api/engines/domains": "owner-only service transition gate: start a transition, read one, read the gate and its lease counts, resolve a held step; tests/test_transition_admin.py covers them",
    "/admin/api/me/retention/ledger": "owner/admin offline retention ledger; tests/test_retention_slice6.py covers scope and no network reads",
    "/admin/api/categories/plan-reach": "the access plan editor's before-Save preview of who a change to a plan's categories reaches (owner only; it reads the rules and the plan and writes nothing); tests/test_category_audience.py covers it",
    "/admin/api/requests/{request_id}/retention/ledger": "dated ledger for a request receipt; tests/test_retention_slice6.py covers revisions",
    "/admin/api/config": "owner-only config sections: effective values with sources, set and reset with If-Match, history, revert, drift, import and export, legacy-file import (docs/design/CONFIG_HOMES.md sections 2-7); tests/test_config_api.py covers auth, preconditions and the round trip",
    "/admin/api/settings/retention/ledger": "owner/admin retention settings ledger; tests/test_retention_slice6.py covers it",
    "/admin/api/settings/retention/systems/{system_id}/facts": "owner-edited system retention facts; tests/test_retention_slice6.py covers removal",
    "/admin/api/engines/{service_id}/retention/ledger": "engine ledger; tests/test_retention_slice6.py covers it",
    "/admin/api/engines/{service_id}/retention/facts": "owner-edited engine facts; tests/test_retention_slice6.py covers it",
    "/admin/api/providers/{provider_id}/retention/facts": "owner-edited provider facts; tests/test_retention_slice6.py covers it",
    "/admin/api/engine-protocols": "validated installed protocol choices for the engine form; tests/test_engine_protocol_admin.py covers seed and user data",
    "/admin/api/engines/{service_id}/stt-measurements": "owner-only long-audio measurement options and job lifecycle; tests/test_stt_measurement.py covers owner gates, bounds and durable progress",
    "/admin/api/engines/{service_id}/stt-settings": "owner-only long-audio settings with registry compare-and-swap; tests/test_stt_measurement.py covers validation and persistence",
    "/native": "per-engine native pass-through (contract section 5): one route, off unless an "
               "engine record sets native_passthrough, outside the OpenAPI schema",
    "/admin/api/operator/host-commands": "owner-only permission for engines that run host commands "
                                         "(contract D13); tests/test_admin_owner_holes_round4.py covers it",
    "/admin/api/settings/sessions": "owner settings for sign-in sessions (idle timeout, absolute lifetime, "
                                    "step-up window); tests/test_session_settings_api.py covers it",
    "/auth/api/reauth": "password confirmation for risky owner actions (step-up); tests/test_session_lifecycle.py covers it",
    "/auth/api/sign-out-everywhere": "ends all of a person's sessions; tests/test_session_lifecycle.py covers it",
    "/auth/api/remembered": "the names a browser keeps for its sign-in list (GET) and the forget route (DELETE "
                            "/auth/api/remembered/{tile}); tests/test_remembered_people.py covers them",
    "/login/provider-sign-out": "sign out at the sign-in provider (A2): a browser redirect outside the OpenAPI schema; "
                                "tests/test_exposure_oidc_security.py covers it",
    "/admin/api/operator/service-registry/excluded": "fix or remove a record the registry leaves out (decision D22); "
                                                     "tests/test_excluded_records.py covers it",
    "/me/api/media/remove-missing": "Library 'Remove missing items' (r64), outside the OpenAPI schema; "
                                    "tests/test_library_remove_missing.py covers it",
    "/me/api/media/confirm-folder": "the owner's confirm of the media marker, called by Library 'Remove missing items' (D17), "
                                    "outside the OpenAPI schema; tests/test_library_remove_missing.py covers it",
    "/me/api/profile": "a person's avatar choice, the sign-in picture served from the gateway and the choices a "
                              "viewer may see, outside the OpenAPI schema; tests/test_me_avatar.py covers them",
    "/admin/api/apps/{app_id}/keys/{credential_id}": "an administrator or the owner renames an App's key (a label, no "
                                                      "new credential, so no step-up); tests/test_rename_apps_keys.py covers it",
    "/gateway/v1/requests": "the maker's own kept requests: list, read, delete and delete all (REQUEST_RETENTION.md section 10); "
                            "a delegated credential without the privacy permission cannot delete; tests/test_request_history_routes.py covers them",
    "/gateway/v1/me/retention": "a delegated App key or MCP grant reads the person's request history choice; writes need privacy permission; tests/test_request_history_routes.py covers it",
    "/me/api/retention": "the member's own request history choice, session only (D50: a member can turn history off); "
                         "tests/test_request_history_member_choice.py covers it",
    "/me/api/session/end-token": "a lightweight read for the current session's CSRF token and name before sign-out; "
                                  "tests/test_session_lifecycle.py covers the endpoint",
    "/me/api/credentials/{credential_id}/privacy-permission": "the accountable person changes request-history access for one of their own App keys after step-up; tests/test_request_history_privacy_permission.py covers it",
    "/me/api/mcp/connections/{grant_id}/privacy-permission": "the signed-in person changes request-history access for one of their own MCP grants after step-up; tests/test_request_history_privacy_permission.py covers it",
}

# Deliberate additions of one method to a path that was already frozen: (method, path template) -> reason. The path
# was frozen answering that method with the 405 of its other methods; each is a route of its own now.
ADDED_METHODS: dict[tuple[str, str], str] = {
    ("PATCH", "/me/api/apps/{app_id}"): "a person renames their own App (outside the OpenAPI schema); "
                                         "tests/test_rename_apps_keys.py covers it",
    ("PATCH", "/me/api/credentials/{credential_id}"): "a person renames their own key's label (outside the OpenAPI schema); "
                                                       "tests/test_rename_apps_keys.py covers it",
    ("POST", "/me/api/apps/{app_id}/trusted-addresses"): "a person trusts an address for their own App (outside the OpenAPI "
                                                         "schema); tests/test_me_trusted_addresses.py covers it",
    ("POST", "/me/api/credentials/{credential_id}/adopt"): "a person keeps an unproven key on their own App (outside the "
                                                           "OpenAPI schema); tests/test_me_binding_adoption.py covers it",
    ("POST", "/me/api/apps/{app_id}/trusted-addresses/{entry_id}/adopt"): "a person keeps an unproven address on their own App "
                                                                          "(outside the OpenAPI schema); tests/test_me_binding_adoption.py covers it",
    ("POST", "/me/api/credentials/{credential_id}/replace"): "a person replaces a key they cannot keep, on their own App (outside "
                                                             "the OpenAPI schema); tests/test_me_binding_adoption.py covers it",
    ("DELETE", "/me/api/apps/{app_id}/trusted-addresses/{entry_id}"): "a person removes a trusted address from their own App "
                                                                     "(outside the OpenAPI schema); tests/test_me_binding_adoption.py covers it",
}


def _template(path: str) -> re.Pattern[str]:
    """A route template, or a probed path made from it, as one pattern: ``{x}`` stands for one segment."""
    return re.compile("/".join("[^/]+" if part.startswith("{") else re.escape(part) for part in path.split("/")))


def _reviewed(path: str | None, methods: list[str] | None = None) -> bool:
    # The owner's secret commands (decision D27) sit under each service: /admin/api/operator/services/{id}/secret-entries.
    if path is not None and path.startswith("/admin/api/operator/services/") and path.endswith("/secret-entries"):
        return True
    if path is not None and methods and all((method, path) in ADDED_METHODS for method in methods):
        return True
    return path is not None and any(
        path == prefix or path.startswith(prefix + "/") or _template(prefix).fullmatch(path)
        for prefix in ADDED_PATHS)


def _frozen() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_fixture_holds_the_whole_graph() -> None:
    frozen = _frozen()
    assert set(frozen) == {"routes", "dispatch", "policy", "openapi_sha256"}
    assert len(frozen["routes"]) > 300
    assert len(frozen["dispatch"]) > 300


def test_every_frozen_route_is_still_registered_the_same_way() -> None:
    live = route_graph.routes(proxy.app)
    frozen_rows = Counter(json.dumps(row, sort_keys=True) for row in _frozen()["routes"])
    live_rows = Counter(json.dumps(row, sort_keys=True) for row in live)

    missing = frozen_rows - live_rows
    assert not missing, f"routes removed or changed: {sorted(missing.elements())[:5]}"
    added = live_rows - frozen_rows
    unreviewed = [row for row in added.elements() if not _reviewed(json.loads(row)["path"], json.loads(row)["methods"])]
    assert not unreviewed, f"routes added without review: {unreviewed[:5]}"


def test_every_frozen_path_dispatches_the_same_way() -> None:
    frozen = _frozen()["dispatch"]
    live = route_graph.capture(proxy)["dispatch"]

    added = {(method, _template(template)) for method, template in ADDED_METHODS}
    changed = {
        (path, method): (outcome, live.get(path, {}).get(method))
        for path, by_method in frozen.items()
        for method, outcome in by_method.items()
        if live.get(path, {}).get(method) != outcome
        and not any(method == added_method and pattern.fullmatch(path) for added_method, pattern in added)
    }
    assert not changed, f"dispatch changed for {len(changed)} requests: {dict(list(changed.items())[:5])}"
    unreviewed = sorted(path for path in set(live) - set(frozen) if not _reviewed(path))
    assert not unreviewed, f"probed paths added without review: {unreviewed[:5]}"


def test_the_protocol_policy_projections_are_unchanged() -> None:
    assert route_graph.policy(proxy) == _frozen()["policy"]


# W5 adds GET /admin/api/engine-protocols for installed description metadata.
# The route is reviewed above; the frozen digest includes its generated schema.
def test_the_openapi_document_is_unchanged(monkeypatch) -> None:
    # MOSS, FLUX.2 Klein, FIBO and keyframe workflows add schemas, never routes or policy.
    # Removing precisely these additions must recover the data-hygiene document,
    # which adds the reviewed missing/retryable output responses, the App key security schemes, and the security each
    # operation declares (API reference security; the route graph leaves that security out of each route's extra).
    added = {"image.generate.moss-image", "image.generate.moss-image-fast",
             "image.generate.moss-image-quality", "image.generate.flux2-klein", "image.edit.flux2-klein", "image.edit.fibo-2-edit-turbo",
             "image.generate.flux2-klein-snofs",
             "video.generate.minimax-h3-keyframes", "video.generate.minimax-h3-keyframes-mid",
             "video.generate.ltx-2.5-keyframes", "video.generate.ltx-2.5-keyframes-mid",
             "mesh.generate.trellis-2"}
    records = proxy.GATEWAY_WORKFLOW_REGISTRY.records()
    with monkeypatch.context() as patch:
        patch.setattr(proxy.GATEWAY_WORKFLOW_REGISTRY, "records",
                      lambda: [record for record in records if record.id not in added])
        proxy.app.openapi_schema = None
        # The edit transport documents repeated image[] parts for pictures. The sign-in box (Login.remember) is
        # nullable: true keeps a name, false takes it off, null leaves the list alone. The remembered list's entry for
        # the person signed in on the browser carries "you": true. The drain write takes the owner's bearer key alone.
        # Re-captured 2026-10-26 after ace-step-1.5-2b-base left the registry.
        # Re-captured 2026-10-28 after the video removal (779db850f): the video paths, schemas and workflow list moved; nothing else.
        # Re-captured 2026-10-30 for POST /v1/meshes/generate (mesh route, discovery and route policy).
        # Re-captured 2026-11-01 for POST /auth/api/clear-browser-cache (see ADDED_PATHS); removing that one path
        # recovers the previous document exactly, so nothing else moved.
        # Re-captured 2026-11-03 for POST /admin/api/categories/plan-reach (see ADDED_PATHS); removing that one path
        # recovers the document with digest edfc855d1cc6a59ba843fb3cfa404ec88ad18963af237f1ccc8b2371faa0db2a exactly.
        # Re-captured 2026-11-05 after the kobold wiring left proxy.py: only the hot-reload route's description moved
        # (it no longer lists KOBOLD_HOST); tests/fixtures/headless_openapi.json shows the same one-line change.
        # Re-captured 2026-11-05 for the config section routes under /admin/api/config (see ADDED_PATHS); removing those
        # paths recovers the document with digest 07410fbacc45a00c2dd669a3213d285d782897a57de979c10c08b01b7f1cb7fe exactly.
        # Re-captured 2026-11-05 for the four /admin/api/engines/domains/... transition routes (see ADDED_PATHS); removing
        # those paths and nothing else changed in the document (tests/fixtures/headless_openapi.json adds the same four).
        assert route_graph.openapi_digest(proxy.app) == "MERGED"
    proxy.app.openapi_schema = None
    # Full document, including the generated schema of the reviewed POST /v1/meshes/generate addition (see ADDED_PATHS).
    # Before that addition the frozen digest was 99fc456823370bfd1bfd469a58deede6a16d0f32b771c0936e3374c7adca4355.
    # Re-captured 2026-10-30: the mesh workflow's picture is a multipart file part and its sync route is documented
    # (WorkflowInputs_mesh_generate_trellis_2 and x-azizam-workflows only; no path or method changed). The reserved
    # extensions input is not an accepted field, so the mesh schema and sync mapping no longer list it.
    # Re-captured 2026-11-01 for POST /auth/api/clear-browser-cache: the headless OpenAPI fixture differs only by
    # that path. Before it the frozen digest was 3656e2ca58e280202f02f0ca19510ed4d962216715930fa6942982a2a90f93d8.
    # Re-captured 2026-11-03 for POST /admin/api/categories/plan-reach: only that path differs. Before it the frozen
    # digest was 1b231e1f19079e480c453aa18b39a49515779462a93338188838e715b242bedd.
    # Re-captured 2026-11-05: the hot-reload description no longer lists KOBOLD_HOST; nothing else moved. Before it the
    # frozen digest was 8dd891cceecfded7e36659aaa598ff4e1b8219ce738a20828dac5923c768670b.
    # Re-captured 2026-11-05 for Prefer: respond-async on POST /v1/meshes/generate: only the mesh workflow's execution
    # entry in x-azizam-workflows moved (it lists the async form and recommends it; no path, method or schema changed;
    # tests/fixtures/headless_openapi.json shows the same change). Before it the frozen digest was
    # 7171e00a6a14ef03826a1e15013d381466881a4c33b3f69fb2b8674137137d0e.
        # Re-captured 2026-11-05 (mesh gate fix): the mesh async entry holds its method, path, content type and headers
        # under ``submit``, the shape every async route has; only that part of x-azizam-workflows moved
        # (tests/fixtures/headless_openapi.json shows the same change). Before it the frozen digest was
        # c31760a71916116e6e65da2f260b796df6295e9125f78a3666f8486ca3ec5168.
        # Re-captured 2026-11-05 for the four /admin/api/engines/domains/... transition routes: only those paths differ.
        # Before it the frozen digest was dce19f7d0e1c01037ee0154b9da207150e8884d5e10470275bec0cbf52fba388.
    assert route_graph.openapi_digest(proxy.app) == _frozen()["openapi_sha256"]
