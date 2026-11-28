"""Every input a workflow advertises is one its request validation accepts.

The MCP ``capabilities`` tool and the workflow catalog advertise each
workflow's inputs; ``run`` builds the request from the same manifest's field
map. An input the gateway sets itself must not be advertised, or a caller who
follows the schema is refused. Checked over every workflow, no names listed.
"""
from __future__ import annotations

import asyncio
import base64
import unittest
from typing import Any, Mapping

import proxy
from gateway.workflow_call import CallError, build_request, effective_schema, validate_inputs

_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16).decode()


def _sample(spec: Mapping[str, Any]) -> Any:
    if "enum" in spec:
        return spec["enum"][0]
    if "default" in spec:
        return spec["default"]
    kind = spec.get("type")
    if spec.get("x-azizam-asset"):
        return f"data:image/png;base64,{_PNG}"
    if kind in ("integer", "number"):
        return spec.get("minimum", 1)
    if kind == "boolean":
        return True
    return "x"


def _routes(entry: Mapping[str, Any]) -> list[tuple[str, dict[str, Any], str | None]]:
    """(mode, route as ``run`` builds it, media-job intent) for every mode the workflow offers."""
    out = []
    execution = entry.get("execution") or {}
    if execution.get("sync"):
        out.append(("sync", execution["sync"], None))
    if execution.get("async"):
        route = execution["async"]
        wrap = route["submit"]["body"]["intent"] if route["kind"] == "media_job" else None
        out.append(("async", {**route["submit"], "fields": route["fields"],
                              "content_type": route["submit"].get("content_type", "")}, wrap))
    return out


class AdvertisedInputsAcceptedTests(unittest.TestCase):
    def test_every_advertised_input_is_accepted(self) -> None:
        catalog = proxy._mcp_catalog()
        self.assertGreater(len(catalog), 10)
        for entry in catalog:
            schema = effective_schema(entry)
            props = schema.get("properties") or {}
            for name, spec in props.items():
                inputs = {name: _sample(spec)}
                for partner in ("width", "height"):
                    # Size is two inputs that travel together.
                    if name in ("width", "height") and partner in props:
                        inputs[partner] = _sample(props[partner])
                for required in schema.get("required") or []:
                    inputs.setdefault(required, _sample(props[required]))
                for mode, route, wrap in _routes(entry):
                    with self.subTest(workflow=entry["id"], input=name, mode=mode):
                        validate_inputs(schema, inputs)
                        try:
                            asyncio.run(build_request(lambda *_: b"", None, None, entry, route, inputs,
                                                      wrap_intent=wrap))
                        except CallError as exc:
                            self.fail(f"{entry['id']} advertises {name!r} but {mode} refuses it: {exc}")


if __name__ == "__main__":
    unittest.main()
