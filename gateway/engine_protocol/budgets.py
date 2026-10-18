"""Budgets of the http-v1 grammar. Static ones are checked when a description loads, the rest on every call."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Budgets:
    # Checked at load: a description that exceeds them is refused.
    max_rules: int = 64               # mapping rules in one map, nested ones included
    max_when_depth: int = 3           # how deep conditions nest
    max_constant_bytes: int = 16 * 1024
    # Checked on every call.
    max_json_depth: int = 32          # nesting of an upstream JSON answer and of a mapped body
    max_request_bytes: int = 8 * 1024 * 1024
    max_upload_bytes: int = 64 * 1024 * 1024        # all file parts of one multipart body
    max_response_bytes: int = 16 * 1024 * 1024      # a buffered JSON answer
    max_asset_bytes: int = 256 * 1024 * 1024        # one binary answer saved as an asset
    max_fetch_bytes: int = 512 * 1024 * 1024        # all assets of one fetch_assets call
    max_event_bytes: int = 1024 * 1024              # one SSE event or NDJSON line
    max_stream_bytes: int = 64 * 1024 * 1024        # a whole stream
    max_selected: int = 10_000        # values a wildcard pointer may collect
    max_assets: int = 64              # assets one job may fetch
    max_redirects: int = 3            # same-origin hops; any cross-origin hop is refused
    mapping_s: float = 0.5            # wall time one mapping may take


DEFAULT_BUDGETS = Budgets()
