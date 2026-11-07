"""The policy facts of a workflow record (CODE_TO_DATA_S3, D2): who may run it and at what cost.

A workflow document never holds these (``exposure``, ``trust_tier``, ``resource_tier``, ``tags``). They are record data. The
release ships them in ``registry/workflow_policy.yml``, keyed by workflow id; an owner changes them as a patch on the
record. **There are no defaults.** A record that states no facts is "needs configuration": every reader treats it as
closed, and a run sends nothing to any engine. This is the plan's rule that a missing authorization or resource
declaration never means free admission. The meaning of the values belongs to the policy slice (S5); this module only
carries them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

POLICY_PATH = Path(__file__).resolve().parent.parent / "registry" / "workflow_policy.yml"
FACT_NAMES = ("exposure", "trust_tier", "resource_tier", "tags")
NEEDS_CONFIGURATION_CODE = "needs_configuration"
NEEDS_CONFIGURATION_MESSAGE = "This workflow needs configuration: its policy facts are not set."


class PolicyFileError(ValueError):
    """The release policy file does not load or breaks its own rules."""


class NeedsConfiguration(Exception):
    """A workflow was asked to run, or to be judged, without policy facts. Nothing was sent to any engine."""

    code = NEEDS_CONFIGURATION_CODE

    def __init__(self, workflow_id: str | None = None) -> None:
        super().__init__(NEEDS_CONFIGURATION_MESSAGE)
        self.workflow_id = workflow_id


@dataclass(frozen=True)
class PolicyFacts:
    """The four facts of one record. All are stated: a record that cannot state them has no ``PolicyFacts`` at all."""

    exposure: str
    trust_tier: str
    resource_tier: str
    tags: tuple[str, ...] = ()


def facts_from_mapping(raw: Any) -> PolicyFacts | None:
    """The facts a mapping states, or ``None`` when it does not state all four (or any is malformed). It never fills one in."""
    if not isinstance(raw, Mapping) or any(name not in raw for name in FACT_NAMES):
        return None
    texts = [raw[name] for name in ("exposure", "trust_tier", "resource_tier")]
    tags = raw["tags"]
    if not all(isinstance(text, str) and text.strip() for text in texts):
        return None
    if not isinstance(tags, (list, tuple)) or not all(isinstance(tag, str) and tag.strip() for tag in tags) \
            or len(set(tags)) != len(tags):
        return None
    return PolicyFacts(exposure=texts[0], trust_tier=texts[1], resource_tier=texts[2], tags=tuple(tags))


def parse_policy_text(text: str, source: str = "workflow_policy.yml") -> dict[str, PolicyFacts]:
    """``{workflow id: facts}`` from the text of a release policy file. Every row must state all four facts; the file holds
    only ``schema_version`` and ``workflows``."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise PolicyFileError(f"{source} does not parse: {error}") from error
    if not isinstance(document, Mapping) or set(document) != {"schema_version", "workflows"}:
        raise PolicyFileError(f"{source} must hold exactly schema_version and workflows")
    if document["schema_version"] != 1 or isinstance(document["schema_version"], bool):
        raise PolicyFileError(f"{source} has an unsupported schema_version")
    rows = document["workflows"]
    if not isinstance(rows, Mapping):
        raise PolicyFileError(f"{source}: workflows must be a mapping of workflow id to facts")
    found: dict[str, PolicyFacts] = {}
    for workflow_id, raw in rows.items():
        if not isinstance(workflow_id, str) or not workflow_id:
            raise PolicyFileError(f"{source}: a workflow id must be text")
        if not isinstance(raw, Mapping) or set(raw) != set(FACT_NAMES):
            raise PolicyFileError(f"{source}: {workflow_id} must state exactly {', '.join(FACT_NAMES)}")
        facts = facts_from_mapping(raw)
        if facts is None:
            raise PolicyFileError(f"{source}: {workflow_id} has a fact that is not valid text (tags: a list of distinct text)")
        found[workflow_id] = facts
    return found


@lru_cache(maxsize=4)
def _load(path: str, mtime_ns: int, size: int) -> dict[str, PolicyFacts]:
    del mtime_ns, size  # part of the key: a changed file loads again
    return parse_policy_text(Path(path).read_text(encoding="utf-8"), Path(path).name)


def release_policy(path: Path | None = None) -> dict[str, PolicyFacts]:
    """The facts the release ships, by workflow id. A file that is missing or broken raises ``PolicyFileError``."""
    target = Path(path or POLICY_PATH)
    try:
        stat = target.stat()
    except OSError as error:
        raise PolicyFileError(f"{target.name} cannot be read: {error}") from error
    return dict(_load(str(target), stat.st_mtime_ns, stat.st_size))


def release_facts(workflow_id: str, path: Path | None = None) -> PolicyFacts | None:
    """The facts the release ships for one workflow, or ``None`` (the workflow then needs configuration)."""
    try:
        return release_policy(path).get(workflow_id)
    except PolicyFileError:
        return None   # a policy file that cannot be read states nothing: every workflow needs configuration
