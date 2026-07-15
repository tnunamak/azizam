"""Fake-backend integration tests for the gateway-side exact context preflight.

Covers the current-loaded text chat execution path
(``proxy._forward_current_loaded_openai_request`` / ``_exact_preflight_check``):
reuse of the exact final execution body against Bee's ``/apply-template`` +
``/tokenize``, the fail-closed availability contract, the explicit
capability-data gate, positive text-only eligibility, canonical output
allowance precedence, the input>=context_window boundary, cross-hop
cancellation during header waits, the shared wall-clock deadline, no prompt
leakage, and the composed default -> LiteLLM -> internal-current -> fake-Bee
oracle proving stock-Pi-recognized overflow with zero generation calls.

The fake backend is a single ``send_llm_execution_request`` stand-in keyed by
URL path, so every test drives the real route wiring in ``proxy.py`` (no
network) while asserting on the literal bytes sent to each endpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import proxy  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from gateway.backends.exact_preflight import (  # noqa: E402
    check_context_fit,
    is_text_only_chat_body,
    resolve_output_allowance,
)

_VALID_EXACT_PREFLIGHT_CAPABILITY = {
    "apply_template_path": "/apply-template",
    "tokenize_path": "/tokenize",
    "output_limit_precedence": ["n_predict", "max_completion_tokens", "max_tokens"],
}


def _kobold_backends_cfg(overlay: dict, *, remove_keys: tuple[str, ...] = ()) -> dict:
    """Overlay onto the REAL kobold backend record (proxy_config.yml).

    model: "current" alias resolution reads that record's `models:` policy
    list — a bare fake dict for "kobold" would break resolution before the
    preflight is ever reached, so every test overlays onto the real record
    instead of replacing it wholesale. ``remove_keys`` drops keys the real
    record already carries (e.g. its seeded ``exact_preflight`` block) so a
    test can assert the "absent" case honestly, not just override it.
    """
    real_kobold_cfg = dict((proxy.config.get("backends") or {}).get("kobold") or {})
    for key in remove_keys:
        real_kobold_cfg.pop(key, None)
    real_kobold_cfg.update(overlay)
    return {**(proxy.config.get("backends") or {}), "kobold": real_kobold_cfg}


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
        apply_template_delay: float = 0.0,
    ) -> None:
        self.calls: list[proxy.LlmExecutionRequest] = []
        self.input_tokens = input_tokens
        self.apply_template_status = apply_template_status
        self.tokenize_status = tokenize_status
        self.apply_template_payload = apply_template_payload
        self.tokenize_payload = tokenize_payload
        self.completion_payload = completion_payload or _default_completion_body()
        self.raise_on_tokenize = raise_on_tokenize
        self.apply_template_delay = apply_template_delay
        self.completion_calls = 0

    async def send(self, execution_request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
        self.calls.append(execution_request)
        path = execution_request.url.split("?", 1)[0]
        if path.endswith("/apply-template"):
            if self.apply_template_delay:
                await asyncio.sleep(self.apply_template_delay)
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


async def _async_post(path: str, json_body: dict, *, headers: dict | None = None) -> httpx.Response:
    """Drive the app on the CURRENT event loop (no thread/loop crossing).

    ``TestClient`` is sync and spins its own event loop per call in a worker
    thread when awaited from ``asyncio.to_thread`` — cross-loop, so an
    ``asyncio.Event``-based cancellation signal set from the calling loop
    never wakes a waiter on the app's loop (confirmed by direct
    measurement: the signal fires in ~25ms but the waiter only unblocks at
    the fake's own fallback timeout). Cancellation tests need same-loop
    delivery, so they drive the app directly via ``httpx.ASGITransport``.
    """
    transport = httpx.ASGITransport(app=proxy.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=json_body, headers=headers or {})


class TestExactPreflight(unittest.TestCase):
    def _client(self) -> TestClient:
        return TestClient(proxy.app)

    def _patches(
        self,
        backend: _FakeBeeBackend,
        *,
        context_window: int = 100,
        capability: object = _VALID_EXACT_PREFLIGHT_CAPABILITY,
    ):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(proxy, "AUTH_ENABLED", False))
        stack.enter_context(patch.object(proxy, "LLM_BACKEND_HOSTS", {"kobold": "http://127.0.0.1:5051"}))
        overlay = {"exact_preflight": capability} if capability is not None else {}
        stack.enter_context(
            patch.object(
                proxy,
                "config",
                {
                    **proxy.config,
                    "backends": _kobold_backends_cfg(
                        overlay, remove_keys=() if capability is not None else ("exact_preflight",)
                    ),
                },
            )
        )
        stack.enter_context(patch.object(proxy, "get_active_backend", AsyncMock(return_value="kobold")))
        stack.enter_context(
            patch.object(proxy, "current_llm_model_for_backend", AsyncMock(return_value="Manual-Experiment.gguf"))
        )
        stack.enter_context(patch.object(proxy, "_service_availability_response", lambda *_a, **_k: None))
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

    def test_input_at_context_boundary_overflows_even_with_zero_output_allowance(self) -> None:
        # Independent guarantee: input == context_window must overflow even
        # when the request declares max_tokens: 0 (allowance 0), because Bee
        # rejects prompt input >= slot.n_ctx before generation regardless.
        backend = _FakeBeeBackend(input_tokens=100)
        with self._patches(backend, context_window=100):
            response = self._post(
                self._client(),
                {
                    "model": "current",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 0,
                },
            )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "context_length_exceeded")
        self.assertEqual(backend.completion_calls, 0)

    def test_input_just_under_context_boundary_with_zero_allowance_fits(self) -> None:
        backend = _FakeBeeBackend(input_tokens=99)
        with self._patches(backend, context_window=100):
            response = self._post(
                self._client(),
                {
                    "model": "current",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 0,
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)

    def test_n_predict_precedence_over_max_tokens_is_the_checked_allowance(self) -> None:
        # Bee's real precedence (server-task.cpp): n_predict overrides
        # max_completion_tokens overrides max_tokens. A body with both
        # max_tokens: 10 and n_predict: 50 against 80 input / 100 window
        # must be measured as 80 + 50 = 130 (overflow), not 80 + 10 = 90 (fit).
        backend = _FakeBeeBackend(input_tokens=80)
        with self._patches(backend, context_window=100):
            response = self._post(
                self._client(),
                {
                    "model": "current",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 10,
                    "n_predict": 50,
                },
            )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "context_length_exceeded")
        self.assertEqual(backend.completion_calls, 0)

    def test_n_predict_precedence_conflict_that_actually_fits_is_forwarded(self) -> None:
        # Same conflicting fields, but the n_predict-winning allowance fits:
        # prove the precedence resolution isn't just conservative-always.
        backend = _FakeBeeBackend(input_tokens=40)
        with self._patches(backend, context_window=100):
            response = self._post(
                self._client(),
                {
                    "model": "current",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 999,  # would overflow if wrongly checked
                    "n_predict": 10,  # the real winning allowance; 40+10=50 fits
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)

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

    # -- shared 20s wall-clock deadline -----------------------------------

    def test_shared_deadline_bounds_both_calls_combined_not_per_call(self) -> None:
        # apply-template alone takes longer than the shared deadline: the
        # preflight must fail closed, not merely rely on separate per-call
        # budgets that could each individually still be under 20s.
        backend = _FakeBeeBackend(input_tokens=5, apply_template_delay=0.05)
        with self._patches(backend), patch.object(proxy, "PREFLIGHT_WALL_CLOCK_DEADLINE", 0.01):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "preflight_unavailable")
        self.assertEqual(backend.completion_calls, 0)

    def test_deadline_exhausted_before_second_call_fails_closed(self) -> None:
        # apply-template alone consumes nearly the whole deadline; tokenize
        # must not get a fresh full budget — the remaining shared deadline
        # must already be exhausted by the time it would run.
        backend = _FakeBeeBackend(input_tokens=5, apply_template_delay=0.03)
        with self._patches(backend), patch.object(proxy, "PREFLIGHT_WALL_CLOCK_DEADLINE", 0.02):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "preflight_unavailable")

    def test_within_deadline_both_calls_still_complete_normally(self) -> None:
        backend = _FakeBeeBackend(input_tokens=5)
        with self._patches(backend, context_window=1_000_000), patch.object(
            proxy, "PREFLIGHT_WALL_CLOCK_DEADLINE", 20.0
        ):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)

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

    def test_registry_only_cancel_stops_apply_template_header_wait(self) -> None:
        """A cross-hop cancel (registry signal, no local ASGI disconnect)
        arriving while /apply-template awaits headers must stop the
        preflight promptly via the cancellation TOKEN passed to dispatch()
        — not only the local disconnect watcher. Exercises
        _OuterCancellationSpan.dispatch's cancellation_token wiring.

        Drives the app via httpx.ASGITransport (same event loop as the
        signaller) rather than the sync TestClient: a signal fired from a
        different event loop than the one awaiting cancellation_token.wait()
        never wakes that waiter (asyncio.Event is not cross-loop-safe),
        which would silently mask this exact regression by falling through
        to the fake's own fallback timeout instead of the real signal.
        """
        registry = proxy.cancellation_registry()
        captured_request_id: list[str] = []
        woke_by_signal = asyncio.Event()

        async def stalls_then_cancelled(execution_request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
            self.assertIsNotNone(cancellation_token, "dispatch() must pass a cancellation_token")
            wait_task = asyncio.ensure_future(cancellation_token.wait())
            done, pending = await asyncio.wait({wait_task}, timeout=2.0)
            self.assertIn(wait_task, done, "registry signal did not wake dispatch's header wait")
            woke_by_signal.set()
            if on_signal is not None:
                await on_signal()
            raise proxy.ClientDisconnectedBeforeHeaders()

        real_span_init = proxy._OuterCancellationSpan.__init__

        def capturing_init(self, request_id, *, receive):
            captured_request_id.append(request_id)
            real_span_init(self, request_id, receive=receive)

        backend = _FakeBeeBackend()
        with self._patches(backend), patch.object(
            proxy, "send_llm_execution_request", stalls_then_cancelled
        ), patch.object(proxy._OuterCancellationSpan, "__init__", capturing_init):

            async def signal_when_ready():
                for _ in range(200):  # up to ~2s of polling for the span to exist
                    if captured_request_id:
                        registry.signal(captured_request_id[0])
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("span was never constructed within the poll window")

            async def run():
                signaller = asyncio.ensure_future(signal_when_ready())
                response = await _async_post(
                    "/v1/chat/completions",
                    {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
                )
                await signaller
                return response

            response = asyncio.run(run())
        self.assertEqual(response.status_code, 499, response.text)
        self.assertTrue(
            woke_by_signal.is_set(),
            "cancellation_token.wait() must be woken by the registry signal itself, "
            "not merely by the fake's own fallback timeout",
        )

    def test_registry_only_cancel_stops_body_read_with_full_cleanup(self) -> None:
        """Same registry-only cancel signal, but during the body-read phase
        (read_body already races the token — proves the existing wiring
        there, plus that dispatch()+read_body() cleanup and the 499/receipt
        path both complete correctly when only the registry (not local ASGI)
        signals). Uses the same same-event-loop harness as the header-wait
        test above, for the same cross-loop-signal reason.
        """
        registry = proxy.cancellation_registry()
        captured_request_id: list[str] = []
        closed_flags: dict[str, bool] = {}

        class _NeverReadyResponse:
            status_code = 200
            headers = {"content-type": "application/json"}

            async def aread(self):
                await asyncio.Event().wait()

            async def aclose(self):
                closed_flags["response_closed"] = True

        class _TrackedClient:
            async def aclose(self):
                closed_flags["client_closed"] = True

        async def send_then_hang_on_read(execution_request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
            if execution_request.url.endswith("/apply-template"):
                return _TrackedClient(), _NeverReadyResponse()
            raise AssertionError("tokenize must not be reached: apply-template read never completes")

        real_span_init = proxy._OuterCancellationSpan.__init__

        def capturing_init(self, request_id, *, receive):
            captured_request_id.append(request_id)
            real_span_init(self, request_id, receive=receive)

        backend = _FakeBeeBackend()
        with self._patches(backend), patch.object(
            proxy, "PREFLIGHT_WALL_CLOCK_DEADLINE", 5.0
        ), patch.object(
            proxy, "send_llm_execution_request", send_then_hang_on_read
        ), patch.object(proxy._OuterCancellationSpan, "__init__", capturing_init):

            async def signal_when_ready():
                for _ in range(200):  # up to ~2s of polling for the span to exist
                    if captured_request_id:
                        registry.signal(captured_request_id[0])
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("span was never constructed within the poll window")

            async def run():
                signaller = asyncio.ensure_future(signal_when_ready())
                response = await _async_post(
                    "/v1/chat/completions",
                    {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
                )
                await signaller
                return response

            response = asyncio.run(run())
        self.assertEqual(response.status_code, 499, response.text)
        self.assertTrue(closed_flags.get("response_closed"))
        self.assertTrue(closed_flags.get("client_closed"))

    # -- positive text-only eligibility -----------------------------------

    def test_multimodal_image_request_skips_preflight_and_proceeds_unguarded(self) -> None:
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

    def test_input_audio_request_skips_preflight_and_proceeds_unguarded(self) -> None:
        backend = _FakeBeeBackend(input_tokens=999_999)
        with self._patches(backend, context_window=100):
            response = self._post(
                self._client(),
                {
                    "model": "current",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "transcribe"},
                                {
                                    "type": "input_audio",
                                    "input_audio": {"data": "AAAA", "format": "wav"},
                                },
                            ],
                        }
                    ],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)
        self.assertFalse(any(c.url.endswith("/apply-template") for c in backend.calls))

    def test_unknown_content_block_type_skips_preflight_and_proceeds_unguarded(self) -> None:
        backend = _FakeBeeBackend(input_tokens=999_999)
        with self._patches(backend, context_window=100):
            response = self._post(
                self._client(),
                {
                    "model": "current",
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "some_future_block", "data": "??"}],
                        }
                    ],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)
        self.assertFalse(any(c.url.endswith("/apply-template") for c in backend.calls))

    def test_malformed_content_shape_skips_preflight_and_proceeds_unguarded(self) -> None:
        # content is neither string/null nor a list of dict blocks (a bare
        # int) — malformed, not text-only; must not be silently counted.
        backend = _FakeBeeBackend(input_tokens=999_999)
        with self._patches(backend, context_window=100):
            response = self._post(
                self._client(),
                {
                    "model": "current",
                    "messages": [{"role": "user", "content": 12345}],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)
        self.assertFalse(any(c.url.endswith("/apply-template") for c in backend.calls))

    def test_is_text_only_chat_body_unit_cases(self) -> None:
        self.assertTrue(is_text_only_chat_body({"messages": [{"role": "user", "content": "hi"}]}))
        self.assertTrue(is_text_only_chat_body({"messages": [{"role": "user", "content": None}]}))
        self.assertTrue(
            is_text_only_chat_body(
                {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
            )
        )
        self.assertFalse(
            is_text_only_chat_body(
                {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]}
            )
        )
        self.assertFalse(
            is_text_only_chat_body(
                {"messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]}]}
            )
        )
        self.assertFalse(
            is_text_only_chat_body(
                {"messages": [{"role": "user", "content": [{"type": "unknown_block"}]}]}
            )
        )
        self.assertFalse(is_text_only_chat_body({"messages": [{"role": "user", "content": 5}]}))
        self.assertFalse(is_text_only_chat_body({"messages": "not-a-list"}))
        self.assertFalse(is_text_only_chat_body({"messages": ["not-a-dict"]}))
        self.assertFalse(
            is_text_only_chat_body({"messages": [{"role": "user", "content": ["not-a-dict-block"]}]})
        )

    # -- explicit capability data (not state_type) -------------------------

    def test_backend_without_declared_capability_skips_preflight(self) -> None:
        backend = _FakeBeeBackend(input_tokens=999_999)
        with self._patches(backend, context_window=100, capability=None):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)
        self.assertFalse(any(c.url.endswith("/apply-template") for c in backend.calls))

    def test_state_type_llama_cpp_alone_does_not_enable_preflight(self) -> None:
        """The prior REVISE finding: state_type was never an honest endpoint
        declaration. A backend with state_type: llama_cpp but NO
        exact_preflight block must be unguarded."""
        backend = _FakeBeeBackend(input_tokens=999_999)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy, "LLM_BACKEND_HOSTS", {"kobold": "http://127.0.0.1:5051"}
        ), patch.object(
            proxy,
            "config",
            {
                **proxy.config,
                "backends": _kobold_backends_cfg(
                    {"state_type": "llama_cpp"}, remove_keys=("exact_preflight",)
                ),
            },
        ), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(
            proxy, "current_llm_model_for_backend", AsyncMock(return_value="Manual-Experiment.gguf")
        ), patch.object(
            proxy, "_service_availability_response", lambda *_a, **_k: None
        ), patch.object(
            proxy, "_context_window_for_model_id", AsyncMock(return_value=100)
        ), patch.object(proxy, "send_llm_execution_request", backend.send):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)
        self.assertFalse(any(c.url.endswith("/apply-template") for c in backend.calls))

    def test_explicit_capability_enables_preflight_where_state_type_alone_did_not(self) -> None:
        backend = _FakeBeeBackend(input_tokens=999_999)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy, "LLM_BACKEND_HOSTS", {"kobold": "http://127.0.0.1:5051"}
        ), patch.object(
            proxy,
            "config",
            {
                **proxy.config,
                "backends": _kobold_backends_cfg(
                    {"state_type": "llama_cpp", "exact_preflight": _VALID_EXACT_PREFLIGHT_CAPABILITY}
                ),
            },
        ), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(
            proxy, "current_llm_model_for_backend", AsyncMock(return_value="Manual-Experiment.gguf")
        ), patch.object(
            proxy, "_service_availability_response", lambda *_a, **_k: None
        ), patch.object(
            proxy, "_context_window_for_model_id", AsyncMock(return_value=100)
        ), patch.object(proxy, "send_llm_execution_request", backend.send):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        # Overflows because input_tokens=999_999 >> context_window=100 — the
        # point is /apply-template WAS called (capability enabled it).
        self.assertEqual(response.status_code, 400, response.text)
        self.assertTrue(any(c.url.endswith("/apply-template") for c in backend.calls))

    def test_capability_missing_apply_template_path_disables_capability(self) -> None:
        backend = _FakeBeeBackend(input_tokens=999_999)
        capability = {**_VALID_EXACT_PREFLIGHT_CAPABILITY, "apply_template_path": ""}
        with self._patches(backend, context_window=100, capability=capability):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(any(c.url.endswith("/apply-template") for c in backend.calls))

    def test_capability_missing_tokenize_path_disables_capability(self) -> None:
        backend = _FakeBeeBackend(input_tokens=999_999)
        capability = dict(_VALID_EXACT_PREFLIGHT_CAPABILITY)
        del capability["tokenize_path"]
        with self._patches(backend, context_window=100, capability=capability):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)

    def test_capability_non_absolute_path_disables_capability(self) -> None:
        backend = _FakeBeeBackend(input_tokens=999_999)
        capability = {**_VALID_EXACT_PREFLIGHT_CAPABILITY, "apply_template_path": "apply-template"}
        with self._patches(backend, context_window=100, capability=capability):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)

    def test_capability_missing_precedence_disables_capability(self) -> None:
        backend = _FakeBeeBackend(input_tokens=999_999)
        capability = dict(_VALID_EXACT_PREFLIGHT_CAPABILITY)
        del capability["output_limit_precedence"]
        with self._patches(backend, context_window=100, capability=capability):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)

    def test_capability_empty_precedence_disables_capability(self) -> None:
        backend = _FakeBeeBackend(input_tokens=999_999)
        capability = {**_VALID_EXACT_PREFLIGHT_CAPABILITY, "output_limit_precedence": []}
        with self._patches(backend, context_window=100, capability=capability):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)

    def test_capability_duplicate_precedence_field_disables_capability(self) -> None:
        backend = _FakeBeeBackend(input_tokens=999_999)
        capability = {
            **_VALID_EXACT_PREFLIGHT_CAPABILITY,
            "output_limit_precedence": ["max_tokens", "max_tokens"],
        }
        with self._patches(backend, context_window=100, capability=capability):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)

    def test_capability_unknown_precedence_field_disables_capability(self) -> None:
        backend = _FakeBeeBackend(input_tokens=999_999)
        capability = {
            **_VALID_EXACT_PREFLIGHT_CAPABILITY,
            "output_limit_precedence": ["max_tokens", "totally_made_up_field"],
        }
        with self._patches(backend, context_window=100, capability=capability):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)

    def test_capability_wrong_shape_disables_capability(self) -> None:
        backend = _FakeBeeBackend(input_tokens=999_999)
        with self._patches(backend, context_window=100, capability="not-a-mapping"):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)

    def test_capability_uses_operator_declared_custom_paths(self) -> None:
        backend = _FakeBeeBackend(input_tokens=5)
        capability = {
            "apply_template_path": "/custom/render",
            "tokenize_path": "/custom/count",
            "output_limit_precedence": ["max_tokens"],
        }

        async def custom_path_send(execution_request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
            path = execution_request.url.split("?", 1)[0]
            if path.endswith("/custom/render"):
                body = json.loads(execution_request.body.decode())
                return _FakeClient(), _FakeResponse(200, {"prompt": json.dumps(body)})
            if path.endswith("/custom/count"):
                return _FakeClient(), _FakeResponse(200, {"tokens": list(range(5))})
            if path.endswith("/v1/chat/completions"):
                backend.completion_calls += 1
                return _FakeClient(), _FakeResponse(200, _default_completion_body())
            raise AssertionError(f"unexpected path {path!r}")

        with self._patches(backend, context_window=1_000_000, capability=capability), patch.object(
            proxy, "send_llm_execution_request", custom_path_send
        ):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(backend.completion_calls, 1)

    # -- downstream auth reuses the existing header path -------------------

    def test_preflight_calls_use_downstream_auth_headers(self) -> None:
        backend = _FakeBeeBackend(input_tokens=5)
        capability = {
            "apply_template_path": "/apply-template",
            "tokenize_path": "/tokenize",
            "output_limit_precedence": ["max_tokens"],
        }
        auth_backends_cfg = _kobold_backends_cfg(
            {
                "exact_preflight": capability,
                "auth_inject": [{"header": "x-admin-key", "secret_env": "TEST_PREFLIGHT_ADMIN_KEY"}],
            }
        )
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy, "LLM_BACKEND_HOSTS", {"kobold": "http://127.0.0.1:5051"}
        ), patch.object(
            proxy, "config", {**proxy.config, "backends": auth_backends_cfg}
        ), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(
            proxy, "current_llm_model_for_backend", AsyncMock(return_value="Manual-Experiment.gguf")
        ), patch.object(
            proxy, "_service_availability_response", lambda *_a, **_k: None
        ), patch.object(
            proxy, "_context_window_for_model_id", AsyncMock(return_value=1_000_000)
        ), patch.object(proxy, "send_llm_execution_request", backend.send), patch.dict(
            proxy.os.environ, {"TEST_PREFLIGHT_ADMIN_KEY": "sk-preflight-admin"}
        ):
            response = self._post(
                self._client(),
                {"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        apply_call = next(c for c in backend.calls if c.url.endswith("/apply-template"))
        tokenize_call = next(c for c in backend.calls if c.url.endswith("/tokenize"))
        self.assertEqual(apply_call.headers.get("x-admin-key"), "sk-preflight-admin")
        self.assertEqual(tokenize_call.headers.get("x-admin-key"), "sk-preflight-admin")

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


class TestExactPreflightActivationThroughRealRegistryProjection(unittest.TestCase):
    """Proves R1's fix through the REAL DB-registry projection path.

    Every other test in this file patches ``proxy.config``/the capability
    directly with a dict that already contains ``exact_preflight`` — that
    proves the capability's validation/wiring, but not that the block ever
    REACHES runtime config for an existing deployment. On a real, already-
    seeded ``data/admin.db``, ``config["backends"]`` is a one-way projection
    from the DB-owned service registry
    (``service_registry.apply_service_registry_to_config`` /
    ``runtime_backends`` / ``runtime_backend_from_service``); ``proxy.py``'s
    ``_load_effective_runtime_config`` only ever calls
    ``service_registry.ensure_service_registry`` then
    ``service_registry.apply_service_registry_to_config`` — this test drives
    those exact two functions (the real mechanism, not a stand-in for it)
    against an isolated on-disk sqlite DB that simulates a deployment seeded
    BEFORE the ``exact_preflight`` YAML block existed, then reads the
    capability off the projected config with NO patching of
    ``proxy.config``/``proxy._exact_preflight_capability``.
    """

    def test_capability_added_to_yaml_after_first_seed_activates_on_existing_deployment(
        self,
    ) -> None:
        import tempfile

        import yaml
        from gateway import admin_store, service_registry

        real_proxy_config = yaml.safe_load(
            (ROOT / "proxy_config.yml").read_text(encoding="utf-8")
        )

        # Simulate the pre-existing deployment: seed the registry from a
        # variant of the REAL config with the kobold backend's
        # `exact_preflight` block stripped out, exactly as a deployment
        # seeded before this feature shipped would look. Every other field
        # (state_type, models, auth_inject-shaped data, etc.) is untouched,
        # so this is a faithful "day-zero" snapshot of the real backend
        # record, not a synthetic minimal fixture.
        pre_feature_config = json.loads(json.dumps(real_proxy_config))
        del pre_feature_config["backends"]["kobold"]["exact_preflight"]

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "admin.db"
            admin_store.init_db(db_path)

            seeded_registry = service_registry.ensure_service_registry(
                pre_feature_config, path=db_path, actor="startup"
            )
            # Sanity: the pre-feature seed genuinely lacks the block (proves
            # the harness actually simulates an old deployment, not a no-op).
            self.assertNotIn(
                "exact_preflight",
                seeded_registry["services"]["kobold"]["config"],
            )

            # The "operator upgrades the gateway" step: the process reboots
            # with the CURRENT proxy_config.yml (which now declares
            # exact_preflight for kobold), against the SAME pre-existing DB.
            # This is exactly what proxy.py's `_load_effective_runtime_config`
            # does on every boot.
            reloaded_registry = service_registry.ensure_service_registry(
                real_proxy_config, path=db_path, actor="startup"
            )
            projected_config = service_registry.apply_service_registry_to_config(
                real_proxy_config, reloaded_registry
            )

        # Drive the real capability reader against the REAL projected config —
        # no patch.object(proxy, "config", ...) and no patch of
        # _exact_preflight_capability itself.
        with patch.object(proxy, "config", projected_config):
            capability = proxy._exact_preflight_capability("kobold")

        self.assertIsNotNone(
            capability,
            "exact_preflight must activate for kobold once the registry "
            "backfill has run against an existing (pre-feature-seeded) DB",
        )
        self.assertEqual(capability.apply_template_path, "/apply-template")
        self.assertEqual(capability.tokenize_path, "/tokenize")
        self.assertEqual(
            capability.output_limit_precedence,
            ("n_predict", "max_completion_tokens", "max_tokens"),
        )

        # Prove it's not just readable — a guarded request actually engages
        # the preflight against this real, DB-projected config: an
        # over-large input must be rejected BEFORE any completion call, and
        # /apply-template must actually have been hit.
        backend = _FakeBeeBackend(input_tokens=999_999)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy, "LLM_BACKEND_HOSTS", {"kobold": "http://127.0.0.1:5051"}
        ), patch.object(proxy, "config", projected_config), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(
            proxy,
            "current_llm_model_for_backend",
            AsyncMock(return_value="Manual-Experiment.gguf"),
        ), patch.object(
            proxy, "_service_availability_response", lambda *_a, **_k: None
        ), patch.object(
            proxy, "_context_window_for_model_id", AsyncMock(return_value=100)
        ), patch.object(proxy, "send_llm_execution_request", backend.send):
            response = TestClient(proxy.app).post(
                "/v1/chat/completions",
                json={"model": "current", "messages": [{"role": "user", "content": "hi"}]},
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(backend.completion_calls, 0)
        self.assertTrue(
            any(c.url.endswith("/apply-template") for c in backend.calls),
            "the real, DB-projected capability must have engaged the preflight",
        )


class TestResolveOutputAllowanceUnit(unittest.TestCase):
    """Table tests: absent/default, 0, each field alone, conflicts, sentinels."""

    PRECEDENCE = ("n_predict", "max_completion_tokens", "max_tokens")

    def test_absent_all_fields_is_unbounded(self) -> None:
        allowance = resolve_output_allowance({}, precedence=self.PRECEDENCE)
        self.assertTrue(allowance.unbounded)
        self.assertEqual(allowance.tokens, 0)

    def test_max_tokens_only(self) -> None:
        allowance = resolve_output_allowance({"max_tokens": 42}, precedence=self.PRECEDENCE)
        self.assertEqual(allowance.tokens, 42)
        self.assertFalse(allowance.unbounded)

    def test_max_completion_tokens_only(self) -> None:
        allowance = resolve_output_allowance(
            {"max_completion_tokens": 7}, precedence=self.PRECEDENCE
        )
        self.assertEqual(allowance.tokens, 7)

    def test_n_predict_only(self) -> None:
        allowance = resolve_output_allowance({"n_predict": 99}, precedence=self.PRECEDENCE)
        self.assertEqual(allowance.tokens, 99)

    def test_zero_is_a_real_reservation_not_absent(self) -> None:
        allowance = resolve_output_allowance({"max_tokens": 0}, precedence=self.PRECEDENCE)
        self.assertEqual(allowance.tokens, 0)
        self.assertFalse(allowance.unbounded)

    def test_n_predict_wins_over_max_completion_tokens_and_max_tokens(self) -> None:
        allowance = resolve_output_allowance(
            {"max_tokens": 10, "max_completion_tokens": 20, "n_predict": 30},
            precedence=self.PRECEDENCE,
        )
        self.assertEqual(allowance.tokens, 30)

    def test_max_completion_tokens_wins_over_max_tokens(self) -> None:
        allowance = resolve_output_allowance(
            {"max_tokens": 10, "max_completion_tokens": 20}, precedence=self.PRECEDENCE
        )
        self.assertEqual(allowance.tokens, 20)

    def test_none_sentinel_is_unbounded(self) -> None:
        allowance = resolve_output_allowance({"max_tokens": None}, precedence=self.PRECEDENCE)
        self.assertTrue(allowance.unbounded)

    def test_negative_one_sentinel_is_unbounded(self) -> None:
        allowance = resolve_output_allowance({"n_predict": -1}, precedence=self.PRECEDENCE)
        self.assertTrue(allowance.unbounded)

    def test_negative_non_sentinel_falls_through_to_next_field(self) -> None:
        allowance = resolve_output_allowance(
            {"n_predict": -5, "max_tokens": 12}, precedence=self.PRECEDENCE
        )
        self.assertEqual(allowance.tokens, 12)

    def test_non_numeric_falls_through_to_next_field(self) -> None:
        allowance = resolve_output_allowance(
            {"n_predict": "not-a-number", "max_tokens": 8}, precedence=self.PRECEDENCE
        )
        self.assertEqual(allowance.tokens, 8)

    def test_precedence_order_reversed_changes_the_winner(self) -> None:
        # Proves the resolution genuinely follows the declared order, not a
        # hardcoded field priority.
        allowance = resolve_output_allowance(
            {"max_tokens": 10, "n_predict": 30},
            precedence=("max_tokens", "n_predict"),
        )
        self.assertEqual(allowance.tokens, 10)


class TestCheckContextFitUnit(unittest.TestCase):
    def test_fits_below_boundary(self) -> None:
        self.assertIsNone(check_context_fit(input_tokens=50, output_allowance=40, context_window=100))

    def test_input_plus_output_exactly_at_boundary_fits(self) -> None:
        self.assertIsNone(check_context_fit(input_tokens=60, output_allowance=40, context_window=100))

    def test_input_plus_output_over_boundary_overflows(self) -> None:
        self.assertIsNotNone(
            check_context_fit(input_tokens=61, output_allowance=40, context_window=100)
        )

    def test_input_alone_equal_to_context_window_overflows_regardless_of_zero_allowance(self) -> None:
        self.assertIsNotNone(
            check_context_fit(input_tokens=100, output_allowance=0, context_window=100)
        )

    def test_input_one_under_context_window_with_zero_allowance_fits(self) -> None:
        self.assertIsNone(check_context_fit(input_tokens=99, output_allowance=0, context_window=100))

    def test_input_over_context_window_overflows(self) -> None:
        self.assertIsNotNone(
            check_context_fit(input_tokens=150, output_allowance=0, context_window=100)
        )


class TestComposedOracle(unittest.TestCase):
    """default -> LiteLLM-shaped internal-current forward -> fake-Bee oracle.

    Drives the actual `/v1/chat/completions` route with model: "default"
    resolved to the current-loaded internal path exactly as the internal
    LiteLLM hop would reach it (X-AI-Gateway-Request-Id forwarded, receipt
    marked internal), and proves the resulting error is the exact OpenAI
    envelope Pi's stock isContextOverflow()/error-body normalization
    recognizes: HTTP 400, message containing "maximum context length", code
    "context_length_exceeded" — with zero completion calls.
    """

    def test_internal_current_hop_overflow_matches_stock_pi_recognized_envelope(self) -> None:
        backend = _FakeBeeBackend(input_tokens=95)
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(proxy, "AUTH_ENABLED", False))
        stack.enter_context(patch.object(proxy, "LLM_BACKEND_HOSTS", {"kobold": "http://127.0.0.1:5051"}))
        stack.enter_context(
            patch.object(
                proxy,
                "config",
                {
                    **proxy.config,
                    "backends": _kobold_backends_cfg({"exact_preflight": _VALID_EXACT_PREFLIGHT_CAPABILITY}),
                },
            )
        )
        stack.enter_context(patch.object(proxy, "get_active_backend", AsyncMock(return_value="kobold")))
        stack.enter_context(
            patch.object(proxy, "current_llm_model_for_backend", AsyncMock(return_value="Manual-Experiment.gguf"))
        )
        stack.enter_context(patch.object(proxy, "_service_availability_response", lambda *_a, **_k: None))
        stack.enter_context(patch.object(proxy, "_context_window_for_model_id", AsyncMock(return_value=100)))
        stack.enter_context(patch.object(proxy, "send_llm_execution_request", backend.send))

        with stack:
            # Simulate the genuine internal-current hop: TestClient reports
            # client host "testclient" by default, which fails
            # _is_internal_protocol_forward's loopback-host check — override
            # to 127.0.0.1 and present the real internal-forward secret
            # header (_INTERNAL_PROTOCOL_FORWARD_HEADER/_SECRET) alongside
            # the forwarded gateway request id, exactly as an internal
            # protocol-frontend loopback call (Anthropic/Ollama -> chat) or
            # LiteLLM's forward_client_headers_to_llm_api would present it —
            # not just any external caller setting the id header, which the
            # outer route would otherwise ignore (trusted_parent_request_id
            # stays "" unless is_trusted_loopback is genuinely true).
            client = TestClient(proxy.app, client=("127.0.0.1", 54321))
            response = client.post(
                "/v1/chat/completions",
                headers={
                    proxy._INTERNAL_PROTOCOL_FORWARD_HEADER: proxy._INTERNAL_PROTOCOL_FORWARD_SECRET,
                    proxy.GATEWAY_REQUEST_ID_HEADER: "req-composed-oracle-1",
                },
                json={
                    "model": "current",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 50,
                },
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(backend.completion_calls, 0, "no generation call may occur on overflow")
        payload = response.json()
        error = payload["error"]
        self.assertEqual(error["code"], "context_length_exceeded")
        self.assertIn("maximum context length", error["message"])
        # This is the exact string/code shape Pi's official isContextOverflow()
        # regex and OpenAI-completions error-body normalization match (see
        # gateway-exact-preflight-gate-0715.md's "What is proven good" section,
        # citing packages/ai/src/utils/overflow.ts / error-body.ts against this
        # same envelope shape) — re-asserted here as the oracle boundary this
        # revision must keep proving, not just the maker's own claim.
        self.assertEqual(error["type"], "invalid_request_error")


if __name__ == "__main__":
    unittest.main()
