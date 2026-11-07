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
BUNDLED_PACKAGES_DIR = Path(__file__).resolve().parent.parent / "extensions" / "bundled"
PACKAGE_KEY = "package"  # an optional key of a policy row: the id of the one package the row was written for
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


def parse_policy_rows(text: str, source: str = "workflow_policy.yml") -> tuple[dict[str, PolicyFacts], dict[str, str]]:
    """``({workflow id: facts}, {workflow id: package id})`` from the text of a release policy file. Every row must state all
    four facts and may name one ``package``; the file holds only ``schema_version`` and ``workflows``. A row with a package
    belongs to the workflows of that package alone; a row without one belongs to the workflows the release itself holds."""
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
    scopes: dict[str, str] = {}
    for workflow_id, raw in rows.items():
        if not isinstance(workflow_id, str) or not workflow_id:
            raise PolicyFileError(f"{source}: a workflow id must be text")
        if not isinstance(raw, Mapping) or not set(FACT_NAMES) <= set(raw) <= {*FACT_NAMES, PACKAGE_KEY}:
            raise PolicyFileError(f"{source}: {workflow_id} must state exactly {', '.join(FACT_NAMES)} (and may name a package)")
        facts = facts_from_mapping({name: raw[name] for name in FACT_NAMES})
        if facts is None:
            raise PolicyFileError(f"{source}: {workflow_id} has a fact that is not valid text (tags: a list of distinct text)")
        if PACKAGE_KEY in raw:
            if not isinstance(raw[PACKAGE_KEY], str) or not raw[PACKAGE_KEY].strip():
                raise PolicyFileError(f"{source}: {workflow_id} must name its package as text")
            scopes[workflow_id] = raw[PACKAGE_KEY]
        found[workflow_id] = facts
    return found, scopes


def parse_policy_text(text: str, source: str = "workflow_policy.yml") -> dict[str, PolicyFacts]:
    """``{workflow id: facts}`` from the text of a release policy file (see ``parse_policy_rows``)."""
    return parse_policy_rows(text, source)[0]


@lru_cache(maxsize=4)
def _load(path: str, mtime_ns: int, size: int) -> tuple[dict[str, PolicyFacts], dict[str, str]]:
    del mtime_ns, size  # part of the key: a changed file loads again
    return parse_policy_rows(Path(path).read_text(encoding="utf-8"), Path(path).name)


def _loaded(path: Path | None) -> tuple[dict[str, PolicyFacts], dict[str, str]]:
    target = Path(path or POLICY_PATH)
    try:
        stat = target.stat()
    except OSError as error:
        raise PolicyFileError(f"{target.name} cannot be read: {error}") from error
    facts, scopes = _load(str(target), stat.st_mtime_ns, stat.st_size)
    return dict(facts), dict(scopes)


def release_policy(path: Path | None = None) -> dict[str, PolicyFacts]:
    """The facts the release ships for the workflows it holds itself, by workflow id: the rows that name no package. A row
    that names a package is not here (``release_facts`` gives it to that package alone). A file that is missing or broken
    raises ``PolicyFileError``."""
    facts, scopes = _loaded(path)
    return {workflow_id: row for workflow_id, row in facts.items() if workflow_id not in scopes}


def shipped_package_hash(package_id: str, root: Path | None = None) -> str | None:
    """The reviewed hash of the package of this id that the release ships (a directory of ``root``, by default
    ``extensions/bundled``), or ``None`` when it ships none, the folder cannot be read, or the shipped one does not review
    clean. The hash is the installer's own (``extension_packages``): it covers the files the review reads (the package
    manifest and the objects, artifacts and code it references). The release's own folder does not change while the
    gateway runs, so its answer is reviewed once per process (``shipped_package_hash_cache_clear`` forgets it); another
    ``root`` is reviewed on each call."""
    return _release_package_hash(package_id) if root is None else _reviewed_package_hash(package_id, Path(root))


@lru_cache(maxsize=None)
def _release_package_hash(package_id: str) -> str | None:
    return _reviewed_package_hash(package_id, BUNDLED_PACKAGES_DIR)


shipped_package_hash_cache_clear = _release_package_hash.cache_clear


def _reviewed_package_hash(package_id: str, base: Path) -> str | None:
    from gateway.extension_packages import MANIFEST_NAME, review_local_directory_package

    try:
        directories = sorted(base.iterdir()) if base.is_dir() else []
    except OSError:
        return None   # a folder that cannot be listed ships nothing this check can prove
    for directory in directories:
        try:
            declared = (yaml.safe_load((directory / MANIFEST_NAME).read_text(encoding="utf-8")) or {}).get("id")
            if declared == package_id:
                review = review_local_directory_package(directory)
                return str(review["package_hash"]) if review.get("ok") else None
        except (OSError, ValueError, yaml.YAMLError, AttributeError):
            continue
    return None


def release_facts(workflow_id: str, path: Path | None = None, *, package_id: str | None = None,
                  package_hash: str | None = None, from_package: bool = False,
                  packages_root: Path | None = None) -> PolicyFacts | None:
    """The facts the release ships for one workflow, or ``None`` (the workflow then needs configuration).

    The one rule for whose workflow a row belongs to: a workflow that an installed package declares (``from_package``) gets
    a row only when the row names that package (``package_id``, the id the package states) **and** the package is the one
    the release ships: its reviewed hash (``package_hash``, the one the installer recorded) equals that of the shipped
    package of that id. A package id alone proves nothing; the content hash does. A workflow the package does not declare
    gets a row only when the row names no package. So a package that declares the id of another workflow, or the id of the
    shipped package with other content, borrows nothing from the release's file."""
    try:
        facts, scopes = _loaded(path)
    except PolicyFileError:
        return None   # a policy file that cannot be read states nothing: every workflow needs configuration
    scope = scopes.get(workflow_id)
    if from_package:
        belongs = (scope is not None and scope == package_id and bool(package_hash)
                   and package_hash == shipped_package_hash(scope, packages_root))
    else:
        belongs = scope is None
    return facts.get(workflow_id) if belongs else None
