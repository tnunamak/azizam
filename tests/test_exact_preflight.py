"""Fake-backend integration tests for the gateway-side exact context preflight.

Covers the current-loaded text chat execution path
(``proxy._forward_current_loaded_openai_request`` / ``_exact_preflight_check``):
reuse of the exact final execution body against Bee's ``/apply-template`` +
``/tokenize``, the fail-closed availability contract, the multimodal and
capability-gated skip policy, cancellation propagation, and that no prompt
content ever reaches a receipt/log/error.

The fake backend is a single ``send_llm_execution_request`` stand-in keyed by
URL path, so every test drives the real route wiring in ``proxy.py`` (no
network) while asserting on the literal bytes sent to each endpoint.
"""

from __future__ import annotations

import contextlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self._payload = payload
        self.closed = False

    async def aread(self):
        if isinstance(self._payload, (bytes, bytearray)):
            return bytes(self._payload)
        return json.dumps(self._payload).encode()

    async def aclose(self):
        self.closed = True


class _FakeClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self):
        self.closed = True


def _default_completion_body() -> dict:
    return {
        "id": "chatcmpl_test",
        "object": "chat.completion",
        "model": "Manual-Experiment.gguf",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "pong"},
                "finish_reason": "stop",
            }
        ],
    }


class _FakeBeeBackend:
    """Routes fake responses by URL path; records every call verbatim."""

    def __init__(
        self,
        *,
        input_tokens: int = 10,
        apply_template_status: int = 200,
        tokenize_status: int = 200,
        apply_template_payload: object | None = None,
        tokenize_payload: object | None = None,
        completion_payload: object | None = None,
        raise_on_tokenize: Exception | None = None,
    ) -> None:
        self.calls: list[proxy.LlmExecutionRequest] = []
        self.input_tokens = input_tokens
        self.apply_template_status = apply_template_status
        self.tokenize_status = tokenize_status
        self.apply_template_payload = apply_template_payload
        self.tokenize_payload = tokenize_payload
        self.completion_payload = completion_payload or _default_completion_body()
        self.raise_on_tokenize = raise_on_tokenize
        self.completion_calls = 0

    async def send(self, execution_request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
        self.calls.append(execution_request)
        path = execution_request.url.split("?", 1)[0]
        if path.endswith("/apply-template"):
            body = json.loads(execution_request.body.decode())
            prompt = "PROMPT[" + json.dumps(body, sort_keys=True) + "]"
            payload = (
                self.apply_template_payload
                if self.apply_template_payload is not None
                else {"prompt": prompt}
            )
            return _FakeClient(), _FakeResponse(self.apply_template_status, payload)
        if path.endswith("/tokenize"):
            if self.raise_on_tokenize is not None:
                raise self.raise_on_tokenize
            payload = (
                self.tokenize_payload
                if self.tokenize_payload is not None
                else {"tokens": list(range(self.input_tokens))}
            )
            return _FakeClient(), _FakeResponse(self.tokenize_status, payload)
        if path.endswith("/v1/chat/completions"):
            self.completion_calls += 1
            return _FakeClient(), _FakeResponse(200, self.completion_payload)
        raise AssertionError(f"unexpected preflight/execution call to {path!r}")


class TestExactPreflight(unittest.TestCase):
    def _client(self) -> TestClient:
        return TestClient(proxy.app)

    def _patches(self, backend: _FakeBeeBackend, *, context_window: int = 100):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(proxy, "AUTH_ENABLED", False))
        stack.enter_context(patch.object(proxy, "LLM_BACKEND_HOSTS", {"kobold": "http://127.0.0.1:5051"}))
        stack.enter_context(patch.object(proxy, "get_active_backend", AsyncMock(return_value="kobold")))
        stack.enter_context(
            patch.object(proxy, "current_llm_model_for_backend", AsyncMock(return_value="Manual-Experiment.gguf"))
        )
        stack.enter_context(patch.object(proxy, "_service_availability_response", lambda *_a, **_k: None))
        stack.enter_context(patch.object(proxy, "_backend_supports_exact_preflight", lambda _backend: True))
        stack.enter_context(
            patch.object(proxy, "_context_window_for_model_id", AsyncMock(return_value=context_window))
        )
        stack.enter_context(patch.object(proxy, "send_llm_execution_request", backend.send))
        return stack

    def _post(self, client: TestClient, body: dict):
        return client.post("/v1/chat/completions", json=body)

    # -- fit path -----------------------------------------------------

    def test_apply_template_body_equals_final_execution_body(self) -> None:
        backend = _FakeBeeBackend(input_tokens=5)
        with self._patches(backend, context_window=1_000_000):
            response = self._post(
                self._client(),
                {
                    "model": "current",
                    "messages": [{"role": "user", "content": "hi"}],
                    "reasoning_effort": "minimal",
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        apply_call = next(c for c in backend.calls if c.url.endswith("/apply-template"))
        completion_call = next(c for c in backend.calls if c.url.endswith("/v1/chat/completions"))
        self.assertEqual(
            json.loads(apply_call.body.decode()),
            json.loads(completion_call.body.decode()),
        )

    def test_tokenize_uses_add_special_and_parse_special(self) -> None:
        backend = _FakeBeeBackend(input_tokens=5)
        with self._patches(backend, context_window=1_000_000):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        tokenize_call = next(c for c in backend.calls if c.url.endswith("/tokenize"))
        tokenize_body = json.loads(tokenize_call.body.decode())
        self.assertIs(tokenize_body["add_special"], True)
        self.assertIs(tokenize_body["parse_special"], True)
        self.assertIn("content", tokenize_body)

    def test_fitting_request_makes_exactly_one_completion_call(self) -> None:
        backend = _FakeBeeBackend(input_tokens=5)
        with self._patches(backend, context_window=1_000_000):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)

    # -- overflow path --------------------------------------------------

    def test_overflow_rejects_before_any_completion_call(self) -> None:
        backend = _FakeBeeBackend(input_tokens=95)
        with self._patches(backend, context_window=100):
            response = self._post(
                self._client(),
                {
                    "model": "current",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 50,
                },
            )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "context_length_exceeded")
        self.assertEqual(backend.completion_calls, 0)

    def test_output_allowance_is_bound_into_the_fit_check(self) -> None:
        # 60 input tokens alone fit a 100-token window, but +50 requested
        # output does not — the cap must be counted, not just the input.
        backend = _FakeBeeBackend(input_tokens=60)
        with self._patches(backend, context_window=100):
            response = self._post(
                self._client(),
                {
                    "model": "current",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 50,
                },
            )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "context_length_exceeded")

    def test_output_allowance_absent_reserves_nothing_extra(self) -> None:
        # Unit-level: check_context_fit itself treats a 0 output_allowance as
        # "reserve nothing beyond the input" (the caller passes
        # effective_output_limit(body) or 0). Covered at the integration
        # layer by test_fitting_request_makes_exactly_one_completion_call and
        # test_output_allowance_is_bound_into_the_fit_check above — this repo's
        # real kobold `default` model record always injects a max_tokens
        # default via its transform pipeline (see proxy_config.yml), so a
        # truly cap-less final body cannot be produced through the live
        # integration route without special-casing production config.
        from gateway.backends.exact_preflight import check_context_fit

        self.assertIsNone(
            check_context_fit(input_tokens=95, output_allowance=0, context_window=100)
        )
        self.assertIsNotNone(
            check_context_fit(input_tokens=101, output_allowance=0, context_window=100)
        )

    # -- current-loaded / no activation ---------------------------------

    def test_preflight_never_activates_or_swaps_a_model(self) -> None:
        async def forbidden(*_a, **_k):
            raise AssertionError("preflight must not activate/load a model")

        backend = _FakeBeeBackend(input_tokens=5)
        with self._patches(backend, context_window=1_000_000), patch.object(
            proxy, "ensure_target", forbidden
        ), patch.object(proxy, "load_model", forbidden):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)

    # -- streaming -------------------------------------------------------

    def test_overflow_rejects_a_streaming_request_before_any_stream_starts(self) -> None:
        backend = _FakeBeeBackend(input_tokens=95)
        with self._patches(backend, context_window=100):
            response = self._post(
                self._client(),
                {
                    "model": "current",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 50,
                    "stream": True,
                },
            )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(backend.completion_calls, 0)

    def test_fitting_streaming_request_proceeds_to_the_real_stream_dispatch(self) -> None:
        class _StreamResponse(_FakeResponse):
            def __init__(self) -> None:
                super().__init__(200, {})
                self.headers = {"content-type": "text/event-stream"}

            async def aiter_bytes(self):
                yield b"data: [DONE]\n\n"

        backend = _FakeBeeBackend(input_tokens=5)

        original_send = backend.send

        async def send_with_stream(execution_request, **kwargs):
            if execution_request.url.endswith("/v1/chat/completions"):
                backend.completion_calls += 1
                return _FakeClient(), _StreamResponse()
            return await original_send(execution_request, **kwargs)

        with self._patches(backend, context_window=1_000_000), patch.object(
            proxy, "send_llm_execution_request", send_with_stream
        ):
            response = self._post(
                self._client(),
                {
                    "model": "current",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)

    # -- unavailability (fail-closed) ------------------------------------

    def test_missing_context_window_fails_closed_not_forwarded(self) -> None:
        backend = _FakeBeeBackend(input_tokens=5)
        with self._patches(backend, context_window=100), patch.object(
            proxy, "_context_window_for_model_id", AsyncMock(return_value=None)
        ):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "preflight_unavailable")
        self.assertNotEqual(response.json()["error"]["code"], "context_length_exceeded")
        self.assertEqual(backend.completion_calls, 0)

    def test_apply_template_failure_fails_closed_not_forwarded(self) -> None:
        backend = _FakeBeeBackend(apply_template_status=500)
        with self._patches(backend):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "preflight_unavailable")
        self.assertEqual(backend.completion_calls, 0)

    def test_tokenize_transport_failure_fails_closed_not_forwarded(self) -> None:
        backend = _FakeBeeBackend(raise_on_tokenize=ConnectionError("boom"))
        with self._patches(backend):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "preflight_unavailable")
        self.assertEqual(backend.completion_calls, 0)

    def test_malformed_tokenize_response_fails_closed_not_forwarded(self) -> None:
        backend = _FakeBeeBackend(tokenize_payload={"unexpected": "shape"})
        with self._patches(backend):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "preflight_unavailable")
        self.assertEqual(backend.completion_calls, 0)

    # -- cancellation -----------------------------------------------------

    def test_disconnect_before_preflight_headers_propagates_as_cancellation(self) -> None:
        async def disconnecting_send(execution_request, **_kwargs):
            raise proxy.ClientDisconnectedBeforeHeaders()

        backend = _FakeBeeBackend()
        with self._patches(backend), patch.object(
            proxy, "send_llm_execution_request", disconnecting_send
        ):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 499, response.text)

    # -- multimodal policy -------------------------------------------------

    def test_multimodal_request_skips_preflight_and_proceeds_unguarded(self) -> None:
        backend = _FakeBeeBackend(input_tokens=999_999)  # would overflow if counted
        with self._patches(backend, context_window=100):
            response = self._post(
                self._client(),
                {
                    "model": "current",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "describe"},
                                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                            ],
                        }
                    ],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)
        self.assertFalse(any(c.url.endswith("/apply-template") for c in backend.calls))

    # -- capability gate (no blind probing as policy) -----------------------

    def test_backend_without_declared_capability_skips_preflight(self) -> None:
        backend = _FakeBeeBackend(input_tokens=999_999)  # would overflow if counted
        with self._patches(backend, context_window=100), patch.object(
            proxy, "_backend_supports_exact_preflight", lambda _backend: False
        ):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)
        self.assertFalse(any(c.url.endswith("/apply-template") for c in backend.calls))

    def test_state_type_llama_cpp_is_the_declared_capability_for_kobold(self) -> None:
        """The live Bee/kobold target is enable-able through ordinary registry
        data (``state_type: llama_cpp``) already set in the operator's backend
        config — no new alias, dialect, or activation framework required."""
        with patch.object(
            proxy,
            "config",
            {**proxy.config, "backends": {"kobold": {"state_type": "llama_cpp"}}},
        ):
            self.assertTrue(proxy._backend_supports_exact_preflight("kobold"))
        with patch.object(
            proxy,
            "config",
            {**proxy.config, "backends": {"tabby": {"state_type": "tabbyapi"}}},
        ):
            self.assertFalse(proxy._backend_supports_exact_preflight("tabby"))

    # -- no prompt in receipts/logs/errors -----------------------------------

    def test_overflow_error_never_contains_message_content(self) -> None:
        secret_text = "sk-super-secret-user-message-content-marker"
        backend = _FakeBeeBackend(input_tokens=95)
        with self._patches(backend, context_window=100):
            response = self._post(
                self._client(),
                {
                    "model": "current",
                    "messages": [{"role": "user", "content": secret_text}],
                    "max_tokens": 50,
                },
            )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertNotIn(secret_text, response.text)

    def test_preflight_unavailable_error_never_contains_message_content(self) -> None:
        secret_text = "sk-super-secret-user-message-content-marker"
        backend = _FakeBeeBackend(apply_template_status=500)
        with self._patches(backend):
            response = self._post(
                self._client(),
                {
                    "model": "current",
                    "messages": [{"role": "user", "content": secret_text}],
                },
            )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn(secret_text, response.text)


if __name__ == "__main__":
    unittest.main()
