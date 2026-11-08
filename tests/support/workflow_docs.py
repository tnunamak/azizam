"""Schema 2 workflow documents and policy facts for tests that build their own registries (CODE_TO_DATA_S3, S3.5a).

Since S3.5a the loader refuses schema 1, ``capability`` is required, ``trusted``/``source``/``exposure``/``trust_tier``/
``resource_tier``/``tags`` are not document keys, and a record with no policy facts is closed ("needs configuration").
A test that writes workflow documents into a directory therefore loads them with ``policy=OPEN_POLICY`` (or passes
``facts=STANDARD_FACTS`` to ``add_manifest``); a test that wants a closed record omits the facts.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from gateway.workflow_policy import PolicyFacts

STANDARD_FACTS = PolicyFacts(exposure="default", trust_tier="stable", resource_tier="batch", resource_class="media", tags=())


def facts(*, exposure: str = "default", trust_tier: str = "stable", resource_tier: str = "batch",
          resource_class: str = "media", tags: tuple[str, ...] = ()) -> PolicyFacts:
    return PolicyFacts(exposure=exposure, trust_tier=trust_tier, resource_tier=resource_tier, resource_class=resource_class, tags=tags)


class EveryWorkflow(Mapping[str, PolicyFacts]):
    """A policy that gives the same facts to every workflow id (a test directory's ids are not in the release file)."""

    def __init__(self, given: PolicyFacts = STANDARD_FACTS, overrides: Mapping[str, PolicyFacts] | None = None) -> None:
        self._given = given
        self._overrides = dict(overrides or {})

    def __getitem__(self, workflow_id: str) -> PolicyFacts:
        return self._overrides.get(workflow_id, self._given)

    def __iter__(self) -> Iterator[str]:
        return iter(self._overrides)

    def __len__(self) -> int:
        return len(self._overrides)


OPEN_POLICY = EveryWorkflow()
