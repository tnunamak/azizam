"""The multipart and urlencoded body of ``POST /v1/videos`` read as the JSON-shaped dict the rest of the route is written
against (the OpenAI Python SDK always submits a video as multipart, so that ``input_reference`` can be a file)."""

from __future__ import annotations

import base64
import re
from typing import Any

from starlette.requests import Request

from gateway import request_forms, video_inputs
from gateway.image_extensions import WORKFLOW_EXTENSION_NAMESPACE, detect_unknown_top_level_fields


class VideoFormParseError(Exception):
    """A multipart ``/v1/videos`` body that cannot be read. ``param`` names the field when one is to blame."""

    def __init__(self, message: str, *, param: str | None = None) -> None:
        super().__init__(message)
        self.param = param


async def read_video_form_body(request: Request) -> dict[str, Any]:
    """The form as a dict. A file part named ``input_reference``, ``inputs[<name>]`` or
    ``extensions[<workflow namespace>][<name>]`` becomes a ``data:`` URI; the SDK's ``extensions[ns][name]`` text fields
    are rebuilt into the nested mapping."""
    try:
        form = await request_forms.read_form(request, max_part_size=video_inputs.MAX_FORM_PART_BYTES)
    except Exception as exc:
        raise VideoFormParseError(f"Invalid multipart body: {exc}") from exc
    try:
        items: list[tuple[str, Any]] = []
        for name, value in form.multi_items():
            if hasattr(value, "read") and hasattr(value, "filename"):
                workflow_file = re.fullmatch(
                    rf"(?:extensions\[{re.escape(WORKFLOW_EXTENSION_NAMESPACE)}\]|inputs)\[[^\[\]]+\]", name)
                if name != "input_reference" and workflow_file is None:
                    raise VideoFormParseError(detect_unknown_top_level_fields({name: None}, ())[name], param=name)
                try:
                    raw = await value.read()
                except Exception as exc:
                    raise VideoFormParseError(f"failed to read uploaded {name}: {exc}") from exc
                mime = getattr(value, "content_type", None) or "application/octet-stream"
                items.append((name, "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")))
                continue
            items.append((name, value if not hasattr(value, "read") else str(value)))
        try:
            return video_inputs.parse_bracket_form(items)
        except ValueError as exc:
            raise VideoFormParseError(str(exc)) from exc
    finally:
        await form.close()
