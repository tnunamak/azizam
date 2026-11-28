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
from gateway import registry_snapshot as rs

FIXTURE = Path(__file__).parent / "fixtures" / "route_graph_frozen.json"

# Deliberate additions since the freeze: path prefix -> reason.
ADDED_PATHS: dict[str, str] = {
    "/admin/api/engines/process-preview": "owner-only dry run of the command a definition's process_template makes from slot values; stores and runs nothing; "
                                          "tests/test_engine_process_template.py covers it",
    "/admin/api/operator/adapter-uploads": "owner-only switch and size cap for member adapter uploads (read and write, audited, step-up on write); "
                                           "tests/test_adapter_upload_setting.py covers it",
    "/me/api/adapters": "a signed-in member uploads one safetensors adapter into their own Library; refused unless the owner allows it; "
                        "tests/test_adapter_upload_route.py covers it",
    "/admin/api/operator/engine-folders": "owner-only list of folders engines may be started from (read and write, audited, step-up on write); "
                                          "tests/test_engine_process_template.py covers it",
    "/admin/api/engines/{service_id}/lifecycle": "the owner starts, stops or restarts one engine over its lifecycle driver; owner only, audited; "
                                                 "tests/test_engine_lifecycle_route.py covers it",
    "/admin/api/workflow-choices": "owner choice freshness and refresh, outside the public schema; tests/test_choice_owner_review.py covers the routes and binding resolution",
    "/admin/api/subscriptions": "the owner connects, checks and disconnects the gateway's subscription plans, outside the public schema; "
                                "tests/test_subscription_plan_access.py covers owner-only access; "
                                "tests/test_subscription_review_fixes.py covers the finish round trip",
    "/admin/api/problem-reports": "owner-only list, read and edit of agent problem reports and their webhook destinations "
                                  "(docs/design/MCP_PROBLEM_REPORTS.md); tests/test_problem_reports.py covers auth, scrubbing and the round trip",
    "/gateway/v1/problem-reports": "an App key files a problem report without MCP: the same arguments and the same core as the MCP report_problem tool; "
                                   "tests/test_problem_report_rest.py covers it",
    "/admin/api/workflows": "the owner's workflow registry (CODE_TO_DATA_S4_S8 I6): list, check, create, read, replace, delete, state, publish and restore, each with the os-N ETag; admin guard; tests/test_workflow_owner_api.py covers it",
    "/v1/assets": "an App key uploads one image (POST, multipart, media body limit) and names the returned id in a chat image_url part "
                  "instead of carrying the picture; tests/test_chat_image_asset_refs.py covers it",
    "/openai/v1/assets": "the /openai alias of POST /v1/assets; tests/test_chat_image_asset_refs.py covers it",
    "/v1/assets/{asset_id}": "an App key deletes an asset its accountable person made (DELETE; owner-scoped, a signed content URL is no "
                             "right to delete); tests/test_app_asset_delete.py covers it",
    "/openai/v1/assets/{asset_id}": "the /openai alias of DELETE /v1/assets/{asset_id}; tests/test_app_asset_delete.py covers it",
    "/internal/litellm/local": "the gateway lifecycle hop serves completions and embeddings next to chat (one route per endpoint "
                               "in capability_ids.INNER_HOP_ENDPOINTS), and a POST no route serves answers 404 endpoint_not_served "
                               "before the key check; tests/test_litellm_inner_hop_endpoints.py covers them",
    "/auth/api/browser-people": "several sign-ins per browser: the active person lists, switches to and removes the people "
                                "signed in on this browser (CSRF, same browser group, fresh proof for owners and admins); "
                                "tests/test_browser_people.py covers it",
    "/auth/api/clear-browser-cache": "the sign-out page asks the browser to drop cached files without waiting; tests/test_session_asset_urls.py covers it",
    "/me/api/apps/{app_id}/trusted-addresses": "a person trusts an address for their own App; tests/test_me_trusted_addresses.py covers it",
    "/me/api/credentials/{credential_id}/adopt": "a person keeps an unproven key on their own App; tests/test_me_binding_adoption.py covers it",
    "/me/api/apps/{app_id}/trusted-addresses/{entry_id}/adopt": "a person keeps an unproven address on their own App; tests/test_me_binding_adoption.py covers it",
    "/me/api/apps/{app_id}/trusted-addresses/{entry_id}/readd": "a person adds an unkept address again on their own App in one step; tests/test_me_binding_adoption.py covers it",
    "/me/api/credentials/{credential_id}/replace": "a person replaces a key they cannot keep, on their own App; tests/test_me_binding_adoption.py covers it",
    "/me/api/apps/{app_id}/trusted-addresses/{entry_id}": "a person removes a trusted address from their own App; tests/test_me_binding_adoption.py covers it",
    "/gateway/v1/media/jobs/{job_id}/results/{name:path}": "an authenticated read of one published JSON result of a job, from the job's result bundle; every refusal is one 404; tests/test_media_job_results.py covers it",
    "/v1/3d/generations": "the 3D frontend rows over the run core: POST /v1/3d/generations (the LocalAI wire) and POST /v1/3d/generations/glb (the raw GLB wire); they replace POST /v1/meshes/generate; tests/test_adapter_differential_s7_mesh_rows.py covers them",
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
    "/admin/api/engine-protocols": "validated installed protocol choices for the engine form (GET), and one item per description id: GET, PUT and DELETE, owner rows with ETag and If-Match; tests/test_engine_protocol_admin.py covers seed and user data, tests/test_engine_protocol_writer.py the writes",
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
    """A route template, or a probed path made from it, as one pattern: ``{x}`` stands for one segment and ``{x:path}``
    for the rest of the path."""
    return re.compile("/".join(
        (".+" if part.endswith(":path}") else "[^/]+") if part.startswith("{") else re.escape(part) for part in path.split("/")))


def _reviewed(path: str | None, methods: list[str] | None = None) -> bool:
    # The owner's secret commands (decision D27) sit under each service: /admin/api/operator/services/{id}/secret-entries.
    if path is not None and path.startswith("/admin/api/operator/services/") and path.endswith("/secret-entries"):
        return True
    if path is not None and methods and all((method, path) in ADDED_METHODS for method in methods):
        return True
    return path is not None and any(
        path == prefix or path.startswith(prefix + "/") or _template(prefix).fullmatch(path)
        for prefix in ADDED_PATHS)


# Deliberate removals since the freeze: path -> reason. A frozen route, dispatch row, policy row or OpenAPI path for
# one of these is left out of every comparison below.
REMOVED_PATHS: dict[str, str] = {
    "/admin/api/settings/effective": "config homes C10: the sections routes under /admin/api/config replace the effective view",
    "/admin/api/settings/effective/export": "config homes C10: GET /admin/api/config/export is the one export",
    "/v1/meshes/generate": "S7 (builder D4): the 3D rows POST /v1/3d/generations and /v1/3d/generations/glb replace it",
}


def _frozen() -> dict:
    frozen = json.loads(FIXTURE.read_text(encoding="utf-8"))
    frozen["routes"] = [row for row in frozen["routes"] if row["path"] not in REMOVED_PATHS]
    frozen["dispatch"] = {path: outcome for path, outcome in frozen["dispatch"].items() if path not in REMOVED_PATHS}
    return frozen


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
    # The keyframe workflows add schemas, never routes or policy.
    # Removing precisely these additions must recover the data-hygiene document,
    # which adds the reviewed missing/retryable output responses, the App key security schemes, and the security each
    # operation declares (API reference security; the route graph leaves that security out of each route's extra).
    added = {"video.generate.minimax-h3-keyframes", "video.generate.minimax-h3-keyframes-mid",
             "video.generate.ltx-2.5-keyframes", "video.generate.ltx-2.5-keyframes-mid",
             "video.edit.video-mux"}
    records = {record.id: record for record in proxy.GATEWAY_WORKFLOW_REGISTRY.records() if record.id not in added}
    with monkeypatch.context() as patch:
        patch.setattr(proxy, "GATEWAY_REGISTRY_PROVIDER", rs.Provider(rs.build({}, rs.BundledRelease(records, ()), generation=1)))
        proxy.app.openapi_schema = None
        assert route_graph.openapi_digest(proxy.app) == "407d99a0c189a65b4b741d748ca27b28dec2506e94cc6f620fd19202767ffbb0"
    proxy.app.openapi_schema = None
    assert route_graph.openapi_digest(proxy.app) == _frozen()["openapi_sha256"]
