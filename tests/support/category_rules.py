"""Owner category rules for tests: a snapshot of category labels.

A category is a label; access plans choose which categories they include (``plan_for(categories=...)``
in tests/support/access.py). ``gate_at`` routes the process-wide category gate to a store. Patch
``category_enforcement._GATE`` with it (monkeypatch or ``unittest.mock.patch.object``) so the previous
gate comes back afterwards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from gateway import category_enforcement, category_policy as policy
from gateway.category_api import snapshot_to_json
from gateway.category_store import CategoryStore


def rules(categories: Mapping[str, str]) -> policy.PolicyVersion:
    """A rule set that holds only the categories: id -> label. A category carries no audience."""
    return policy.PolicyVersion(
        "rules",
        {key: policy.Category(key, label, values=("included",)) for key, label in categories.items()},
        {}, {}, {},
    )


def activate(path: Path, snapshot: policy.PolicyVersion) -> None:
    store = CategoryStore(path)
    row = store.create_candidate(snapshot_to_json(snapshot), None, "owner-1", "test rules")
    store.activate(row.revision, None)


def gate_at(path: Path) -> category_enforcement.CategoryGate:
    return category_enforcement.CategoryGate(path)
