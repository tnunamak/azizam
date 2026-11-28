"""Run one image request on the old route and on its frontend row, over the same golden rig and the same fake engine.

``observe(tmp_path, route, request, setup, row=...)`` builds one ``GatewayRig`` (the real app, real stores, the scenario
driven fake ComfyUI engine, the recorder), sends the request, and returns an ``Obs``: what the caller saw, what the engine
saw, the gateway's sleeps, and the receipt, job and asset rows the request left. ``row=False`` sends it to the old route
(the path a client uses today). ``row=True`` sends the same bytes to the route's frontend row, served by the real row server
(``gateway.row_routes``) at ``/__row<path>``, which is where a moved route will answer.

The two observations are compared by the parity tests (``tests/test_s7_parity_images.py``), one test per inventory item.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import proxy
from gateway import frontend_shapes, row_routes
from starlette.routing import Route

from tests.support.adapter_differential_rig import GatewayRig

ROW_OF = {
    "/v1/images/generations": "images-generations",
    "/v1/images/edits": "images-edits",
    "/v1/images/variations": "images-variations",
    "/gateway/v1/media/jobs/image-generations": "image-jobs",
    "/v1/audio/generate": "audio-generate",     # the poll-schedule contract test compares these with the image round
    "/v1/videos": "videos-create",
}
ROW_PREFIX = "/__row"


def mount_rows(rig: GatewayRig) -> None:
    """Serve each image row at ``/__row<canonical path>`` with the real row server."""
    rows = {row.id: row for row in frontend_shapes.bundled_rows()}
    routes = []
    for path, row_id in ROW_OF.items():
        row = rows[row_id]
        routes.append(Route(ROW_PREFIX + path, row_routes._handler(proxy._ROW_ROUTE_DEPS, row, frontend_shapes.build_shape(row), path),
                            methods=["POST"]))
    rig.monkeypatch.setattr(proxy.app.router, "routes", [*routes, *proxy.app.router.routes])
    # The run core keeps the job-store path it was first built with: a core an earlier test built must not serve this rig.
    rig.monkeypatch.setattr(proxy, "_workflow_core_cache", None)
    # Likewise the job spool: it keeps the directory it was first built over, which an earlier test's rig removed.
    rig.monkeypatch.setattr(proxy, "_WORKFLOW_JOB_SPOOL", None)


@dataclass
class Obs:
    """What one request left behind."""

    trace: dict[str, Any]
    response: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> int | None:
        return self.trace["caller"][0]["response"].get("status") if self.trace["caller"] else None

    @property
    def headers(self) -> dict[str, str]:
        return {k.lower(): v for k, v in (self.trace["caller"][0]["response"].get("headers") or {}).items()} if self.trace["caller"] else {}

    @property
    def body(self) -> Any:
        return self.trace["caller"][0]["response"].get("body") if self.trace["caller"] else None

    @property
    def engine(self) -> list[tuple[str, str]]:
        return [(r["method"], r["path"]) for r in self.trace["engine_requests"]]

    @property
    def engine_requests(self) -> list[dict[str, Any]]:
        return self.trace["engine_requests"]

    @property
    def sleeps(self) -> list[float]:
        return [s["delay"] for s in self.trace["gateway_sleeps"]]

    @property
    def receipts(self) -> dict[str, Any]:
        return self.trace["receipts"]

    @property
    def jobs(self) -> dict[str, Any]:
        return self.trace["media_jobs"]

    @property
    def assets(self) -> Any:
        return self.trace["assets"]


def observe(tmp_path: Path, path: str, request: dict[str, Any] | Callable[[GatewayRig], Any], setup: Callable[[GatewayRig], None], *,
            row: bool, name: str = "parity", then: Callable[[GatewayRig, Any], Any] | None = None, method: str = "POST") -> Obs:
    """Send ``request`` (keyword arguments of ``GatewayRig.call``) to ``path`` or to its row, and return the observation.
    ``then(rig, response)`` may drive further calls (a poll, a cancel); it runs on the same rig and returns extra facts."""
    suffix = "row" if row else "old"
    box: dict[str, Any] = {}
    with GatewayRig(f"{name}-{suffix}", Path(tmp_path) / suffix) as rig:
        setup(rig)
        if row:
            mount_rows(rig)

        async def drive() -> None:
            target = ROW_PREFIX + path if row else path
            try:
                if callable(request):
                    box["response"] = await request(rig)
                else:
                    box["response"] = await rig.call(method, target, label=f"{suffix} {path}", **request)
                if then is not None:
                    box["extra"] = await then(rig, box["response"])
            except Exception as error:  # noqa: BLE001 - a route that crashes is an observation, not a test error
                box["exception"] = f"{type(error).__name__}: {str(error)[:200]}"

        asyncio.run(drive())
        trace = json.loads(json.dumps(rig.trace()))
    return Obs(trace=trace, response=box.get("response"), extra={**(box.get("extra") or {}), **({"exception": box["exception"]} if "exception" in box else {})})
