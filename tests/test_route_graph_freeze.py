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
    "/admin/api/engine-protocols": "validated installed protocol choices for the engine form; tests/test_engine_protocol_admin.py covers seed and user data",
    "/native": "per-engine native pass-through (contract section 5): one route, off unless an "
               "engine record sets native_passthrough, outside the OpenAPI schema",
    "/admin/api/operator/host-commands": "owner-only permission for engines that run host commands "
                                         "(contract D13); tests/test_admin_owner_holes_round4.py covers it",
    "/admin/api/settings/sessions": "owner settings for sign-in sessions (idle timeout, absolute lifetime, "
                                    "step-up window); tests/test_session_settings_api.py covers it",
    "/auth/api/reauth": "password confirmation for risky owner actions (step-up); tests/test_session_lifecycle.py covers it",
    "/auth/api/sign-out-everywhere": "ends all of a person's sessions; tests/test_session_lifecycle.py covers it",
    "/admin/api/operator/service-registry/excluded": "fix or remove a record the registry leaves out (decision D22); "
                                                     "tests/test_excluded_records.py covers it",
    "/me/api/media/remove-missing": "Library 'Remove missing items' (r64), outside the OpenAPI schema; "
                                    "tests/test_library_remove_missing.py covers it",
    "/me/api/media/confirm-folder": "Library 'This is the right media folder' (owner confirm of the media marker, D17), "
                                    "outside the OpenAPI schema; tests/test_library_remove_missing.py covers it",
    "/me/api/profile": "a person's avatar choice, the sign-in picture served from the gateway and the choices a "
                              "viewer may see, outside the OpenAPI schema; tests/test_me_avatar.py covers them",
    "/admin/api/apps/{app_id}/keys/{credential_id}": "an administrator or the owner renames an App's key (a label, no "
                                                      "new credential, so no step-up); tests/test_rename_apps_keys.py covers it",
}

# Deliberate additions of one method to a path that was already frozen: (method, path template) -> reason. The path
# was frozen answering that method with the 405 of its other methods; each is a route of its own now.
ADDED_METHODS: dict[tuple[str, str], str] = {
    ("PATCH", "/me/api/apps/{app_id}"): "a person renames their own App (outside the OpenAPI schema); "
                                         "tests/test_rename_apps_keys.py covers it",
    ("PATCH", "/me/api/credentials/{credential_id}"): "a person renames their own key's label (outside the OpenAPI schema); "
                                                       "tests/test_rename_apps_keys.py covers it",
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
def test_the_openapi_document_is_unchanged() -> None:
    assert route_graph.openapi_digest(proxy.app) == _frozen()["openapi_sha256"]
