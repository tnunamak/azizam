"""A fake ComfyUI for video route tests: upload, prompt, history, view, queue."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

SOURCE_PROMPT_ID = "aaaaaaaa-0000-0000-0000-000000000001"
NEW_PROMPT_ID = "bbbbbbbb-0000-0000-0000-000000000002"
SOURCE_BYTES = b"\x00\x00\x00\x18ftypmp42-fake-source-clip"


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, content: bytes = b"") -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = content
        self.text = json.dumps(self._payload) if payload is not None else ""
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def succeeded_history(filename: str = "source_00001_.mp4") -> dict:
    return {
        "status": {"completed": True, "status_str": "success", "messages": []},
        "outputs": {"60": {"videos": [{"filename": filename, "subfolder": "video", "type": "output"}]}},
    }


class FakeComfy:
    """Records uploads and submitted graphs; serves history and /view bytes."""

    def __init__(self) -> None:
        self.history: dict[str, dict] = {SOURCE_PROMPT_ID: succeeded_history()}
        self.view_bytes: dict[str, bytes] = {"source_00001_.mp4": SOURCE_BYTES}
        self.uploads: list[tuple[str, bytes, str]] = []
        self.submitted: list[dict] = []
        self.gets: list[str] = []

    def client_class(self) -> type:
        outer = self

        class Client:
            def __init__(self, **_kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "Client":
                return self

            async def __aexit__(self, *_args: Any) -> bool:
                return False

            async def get(self, url: str, **kwargs: Any) -> FakeResponse:
                outer.gets.append(url)
                if "/history/" in url:
                    pid = url.rsplit("/", 1)[-1]
                    return FakeResponse(200, {pid: outer.history[pid]} if pid in outer.history else {})
                if url.endswith("/view"):
                    name = kwargs["params"]["filename"]
                    if name in outer.view_bytes:
                        return FakeResponse(200, content=outer.view_bytes[name])
                    return FakeResponse(404)
                if url.endswith("/queue"):
                    return FakeResponse(200, {"queue_running": [], "queue_pending": []})
                raise AssertionError(f"unexpected GET {url}")

            async def post(self, url: str, **kwargs: Any) -> FakeResponse:
                if url.endswith("/upload/image"):
                    filename, data, mime = kwargs["files"]["image"]
                    outer.uploads.append((filename, data, mime))
                    return FakeResponse(200, {"name": f"up{len(outer.uploads)}_{filename}"})
                if url.endswith("/prompt"):
                    outer.submitted.append(kwargs["json"]["prompt"])
                    return FakeResponse(200, {"prompt_id": NEW_PROMPT_ID})
                raise AssertionError(f"unexpected POST {url}")

        return Client

    def patches(self, *, probe: Any = None) -> list:
        import proxy

        out = [
            patch.object(proxy.httpx, "AsyncClient", self.client_class()),
            patch.object(proxy, "_service_availability_response", return_value=None),
            patch.object(proxy, "COMFY_ENABLED", True),
            patch.object(proxy, "configured_downstream_headers", lambda _ac, _b: {}),
        ]
        if probe is not None:
            out.append(patch.object(proxy.video_inputs, "probe_video", lambda *_a, **_k: probe))
        return out
