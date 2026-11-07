"""What every record-consuming reader of a workflow gives for one record (CODE_TO_DATA_S3, S3.4).

One table of entry points, used twice: once to capture the old output of every bundled workflow into a fixture (run
at the last commit before the readers switched), and by ``test_workflow_readers_v2`` to compare the new output with it.
Nothing here names a schema 2 helper, so the module runs on both sides of the switch.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterable
from typing import Any

from gateway import capabilities, extension_catalog, home_api, workflow_openapi
from gateway.availability import project_capabilities
from gateway.discovery import BUNDLED_WORKFLOWS_DIR, WorkflowRecord, summarize_workflow
from gateway.workflows_catalog import build_workflows_catalog

AUDIENCES = ("client", "admin", "internal")


class _Registry:
    """The one thing ``_workflow_targets`` asks of a registry."""

    def __init__(self, records: list[WorkflowRecord]) -> None:
        self._records = records

    def records(self) -> list[WorkflowRecord]:
        return self._records


def _caps(record: WorkflowRecord) -> list[Any]:
    return [capabilities.capability_from_workflow_record(record, backend=backend) for backend in record.supported_backends]


def plain(value: Any) -> Any:
    """A JSON form that compares equal when the values do: sets sorted, dataclasses as mappings."""
    def encode(item: Any) -> Any:
        if dataclasses.is_dataclass(item) and not isinstance(item, type):
            return dataclasses.asdict(item)
        if isinstance(item, (set, frozenset)):
            return sorted(item, key=str)
        return str(item)
    # A path under the bundled directory is the checkout's, not the workflow's: it must not differ between checkouts.
    text = json.dumps(value, default=encode, sort_keys=True).replace(json.dumps(str(BUNDLED_WORKFLOWS_DIR))[1:-1], "<bundled>")
    return json.loads(text)


def _catalog(record: WorkflowRecord) -> Any:
    return build_workflows_catalog(records=[record], capabilities=_caps(record), base_url="https://example.test")


def _home(record: WorkflowRecord) -> Any:
    out = []
    for cap in _caps(record):
        out.append({"modalities": home_api._required_input_modalities(cap, [record]),
                    "seconds": home_api._typical_seconds(cap, None, [record])})
    return out


CONSUMERS: dict[str, Callable[[WorkflowRecord], Any]] = {
    **{f"summary:{audience}": (lambda r, a=audience: summarize_workflow(r, a)) for audience in AUDIENCES},
    "capabilities": _caps,
    "schema": workflow_openapi.workflow_inputs_schema,
    "schema_form": lambda r: workflow_openapi.workflow_inputs_schema(r, form=True),
    "advertised": workflow_openapi.advertised_inputs_schema,
    "extension_schema": workflow_openapi.extension_inputs_schema,
    "owned": workflow_openapi.gateway_owned_inputs,
    "route_inputs": workflow_openapi.route_inputs,
    "mapping": lambda r: {mode: workflow_openapi.request_mapping(r, mode) for mode in ("sync", "async")},
    "openapi_catalog": lambda r: workflow_openapi.workflow_catalog([r]),
    "member_catalog": _catalog,
    "extension_targets": lambda r: extension_catalog._workflow_targets(_Registry([r])),
    "home": _home,
    "projected": lambda r: project_capabilities(_caps(r), workflow_records=[r]),
}


def read_all(records: Iterable[WorkflowRecord]) -> dict[str, dict[str, Any]]:
    """Every consumer's plain output for every record: ``{workflow id: {consumer: output}}``."""
    return {record.id: {name: plain(consumer(record)) for name, consumer in CONSUMERS.items()} for record in records}


def read_one(record: WorkflowRecord, name: str) -> Any:
    return plain(CONSUMERS[name](record))
