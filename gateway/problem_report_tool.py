"""The ``report_problem`` MCP tool definition: the words agents act on, with no imports.

Kept apart from gateway/problem_reports.py so the MCP adapter can list the tool without loading the store."""
from __future__ import annotations

from typing import Any

CATEGORIES = ("failure", "differs_from_description", "workaround", "missing_capability")
SUMMARY_MAX = 1000
WORKAROUND_MAX = 500

INSTRUCTION = "If a tool fails, misbehaves or forces a workaround, call `report_problem` before you move on."
REPORT_HINT = ("If you work around this or give up, call `report_problem` once to tell the operator. "
               "The gateway attaches the details.")

TOOL: dict[str, Any] = {
    "name": "report_problem",
    "title": "Report a problem to the operator",
    "description": (
        "Report a problem to the operator of this gateway. Call this yourself, without being asked, whenever "
        "(1) a tool call failed, (2) a result differs from what the tool description or `capabilities` promised, or "
        "(3) you used a workaround, a different tool or another path to get the task done. Say what happened and what "
        "you did instead, in plain words. Do not include keys, prompts or personal data: the gateway already attaches "
        "the tool, target, job id, error and caller. One report per distinct problem; skip a failure that went away "
        "on retry. If the reply has `known_workaround`, use it."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": list(CATEGORIES), "default": "failure",
                         "description": "failure: a call failed. differs_from_description: it worked but not as described. "
                                        "workaround: you took another path. missing_capability: nothing here can do it."},
            "summary": {"type": "string", "minLength": 1, "maxLength": SUMMARY_MAX,
                        "description": "What happened, in plain words."},
            "workaround": {"type": "string", "maxLength": WORKAROUND_MAX,
                           "description": "What you did instead, if anything."},
            "target": {"type": "string", "description": "The workflow, alias or `speech` involved, if you know it."},
            "job_id": {"type": "string", "description": "The job id involved, if there is one."},
        },
        "required": ["summary"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
}
