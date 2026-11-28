"""Round 7 of the secret handling: what the review of phase1-r6 (12b344af) found (decision D43).

The rule: the gateway never logs an upstream response body or an upstream error text (the structural test is
``tests/test_no_upstream_text_in_logs.py``). The scrubber is the backup, and is exercised here for each repro.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from urllib.parse import quote, quote_plus

import httpx
import pytest

import proxy
from gateway import lifecycle_drivers, log_scrub, runtime_secrets, secret_refs, secret_store, service_registry
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401
from tests.test_admin_secret_sweep import host_isolated  # noqa: F401
from tests.test_secret_canary_matrix import _etag
from tests.test_secret_review_r3 import error_code, secret_rows  # noqa: F401
from tests.test_signin_who_can_enter import gateway, owner_and_admin  # noqa: F401
from tests.test_url_credentials_round5 import _confirm, _engine_view, _unbound_plant  # noqa: F401
from tests.test_url_credentials_round6 import _reaim
from tests.test_url_origin_binding import CANARY, GOOD, SERVICE, _record, store, wire  # noqa: F401


# ---------------------------------------------------------------------------
# MED 4: the discovery request logs no part of what the upstream answered
# ---------------------------------------------------------------------------

def test_a_refused_model_discovery_logs_a_status_and_a_size_never_the_echoed_key(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_MODEL_DISCOVERY_API_KEY", "R7-DISCOVERY-KEY-VALUE-0001")

    async def echo(self, request):
        key = request.headers["authorization"].removeprefix("Bearer ")
        return httpx.Response(401, json={"error": {"message": f"Incorrect API key provided: {key}"}}, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", echo)
    log_scrub.clear()
    assert asyncio.run(proxy.refresh_openai_model_aliases()) == 1
    err = capsys.readouterr().err
    assert "R7-DISCOVERY-KEY-VALUE-0001" not in err and "Incorrect API key" not in err
    assert "status 401" in err and "application/json" in err and "bytes" in err


def test_a_refused_model_discovery_logs_no_body_even_when_the_key_is_not_registered(monkeypatch, capsys):
    """The rule does not depend on the scrubber: nothing the upstream wrote is in the line."""
    monkeypatch.delenv("OPENAI_MODEL_DISCOVERY_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "R7-OTHER-KEY-VALUE-0002")

    async def refuse(self, request):
        return httpx.Response(403, text="ANYTHING-THE-UPSTREAM-SAID", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    log_scrub.clear()  # nothing registered: the line must still say nothing of the reply
    assert asyncio.run(proxy.refresh_openai_model_aliases()) == 1
    assert "ANYTHING-THE-UPSTREAM-SAID" not in capsys.readouterr().err


def _driver_record():
    record = _record()
    record["config"].update(driver_credential_origin=GOOD)
    return record


def test_the_driver_logs_no_reply_text_and_no_error_text(store, fake_upstreams, capsys):
    from tests.test_url_origin_binding import _serve

    moved = _owner_stored(_driver_record(), store)
    _serve(fake_upstreams, "good.invalid:9",
           {"id": "unload", "match": {"method": "POST", "path": "/v1/model/unload"},
            "response": {"status": 500, "body": [{"text": "UNLOAD-REPLY-TEXT-R7"}]}},
           {"id": "load", "match": {"method": "POST", "path": "/v1/model/load"},
            "response": {"body": [{"text": '{"error": "LOAD-REPLY-TEXT-R7"}'}]}},
           {"id": "verify", "match": {"method": "GET", "path": "/v1/model"}, "response": {"body": [{"text": "VERIFY-REPLY-TEXT-R7"}]}})
    backend = service_registry.runtime_backend_from_service(moved)
    ctx = lifecycle_drivers.SwapContext(loaded_model="old", target_model="wanted")
    assert asyncio.run(lifecycle_drivers.HttpLoadUnloadDriver().swap(backend, {"model_name": "wanted"}, ctx=ctx)) is False
    out = capsys.readouterr().out
    assert "REPLY-TEXT-R7" not in out
    assert "status 500" in out and "bytes withheld" in out


def test_a_connection_error_logs_its_class_and_origin_never_the_url_it_names(store, monkeypatch, capsys):
    moved = _owner_stored(_driver_record(), store)

    async def explode(self, request):
        raise httpx.ConnectError(f"could not connect to {request.url} for the user {CANARY}-never-registered", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", explode)
    backend = service_registry.runtime_backend_from_service(moved)
    ctx = lifecycle_drivers.SwapContext(loaded_model="old", target_model="wanted")
    assert asyncio.run(lifecycle_drivers.HttpLoadUnloadDriver().swap(backend, {"model_name": "wanted"}, ctx=ctx)) is False
    out = capsys.readouterr().out
    assert CANARY not in out and "/v1/model/unload" not in out and "token=" not in out
    assert "ConnectError (http://good.invalid:9)" in out


def _owner_stored(record, db):
    from gateway import record_carriers

    return record_carriers.externalize_service(record, secret_refs.putter(db), reject_ambiguous=True)[0]


def test_the_formatters_say_a_status_a_size_a_class_and_an_origin_and_nothing_else():
    response = httpx.Response(502, headers={"content-type": "text/html; charset=utf-8"}, content=b"<html>SECRET-BODY</html>")
    line = log_scrub.describe_response(response, "refused")
    assert line == "refused: status 502, text/html, 24 bytes" and "SECRET" not in line
    assert log_scrub.describe_bytes("héllo", "stderr") == "stderr: 6 bytes withheld"
    request = httpx.Request("GET", "http://u:p@host.invalid:8080/path?token=T")
    error = httpx.ConnectError("boom", request=request)
    assert log_scrub.describe_error(error) == "ConnectError (http://host.invalid:8080)"
    assert log_scrub.describe_error(ValueError("text with token=T")) == "ValueError"
    assert log_scrub.describe_origin("https://a:b@h.invalid/x?y=z") == "https://h.invalid"


def test_the_structural_checker_fails_on_a_new_log_of_an_upstream_body():
    import ast
    from tests.support import upstream_taint as taint

    tree = ast.parse("async def f(client):\n    r = await client.get('x')\n    print(f'bad {r.text[:200]}')\n")
    scan = taint.Scan("new.py", taint.module_info(tree), taint.is_log_call, traceback_is_sink=True)
    scan.visit(tree)
    assert [what for _, _, what in scan.found] == ["a response or process text"]


# ---------------------------------------------------------------------------
# MED 1-3: the scrubber, as the backup
# ---------------------------------------------------------------------------

def test_a_value_of_9000_characters_is_removed_with_its_tail():
    log_scrub.clear()
    value = "K" * 8990 + "TAIL-CANARY"
    runtime_secrets.remember(value, "big")
    cleaned = runtime_secrets.scrub(f"the server echoed {value} and stopped", limit=100000)
    assert "TAIL-CANARY" not in cleaned and cleaned == "the server echoed [removed] and stopped"


def test_a_value_with_a_quote_is_removed_from_an_echoed_json_body():
    log_scrub.clear()
    value = 'pa"ss\\word/with\nnewline and é'
    runtime_secrets.remember(value, "tricky")
    echoed = json.dumps({"error": f"bad key {value}"})
    for body in (echoed, json.dumps({"error": f"bad key {value}"}, ensure_ascii=False)):
        cleaned = runtime_secrets.scrub(body, limit=1000)
        assert 'pa\\"ss' not in cleaned and "word" not in cleaned, cleaned


def test_the_first_value_survives_513_and_66000_later_registrations():
    log_scrub.clear()
    runtime_secrets.remember("R7-FIRST-VALUE-SECRET", "ref:first")
    for index in range(513):
        runtime_secrets.remember(f"R7-LATER-{index:06d}-SECRET", f"ref:{index}")
    assert "R7-FIRST-VALUE-SECRET" not in runtime_secrets.scrub("echo R7-FIRST-VALUE-SECRET")
    for index in range(513, 66000):
        runtime_secrets.remember(f"R7-LATER-{index:06d}-SECRET", f"ref:{index}")
    assert len(log_scrub.sources()) == 66001, "sources are bounded by configuration, nothing is evicted"
    assert "R7-FIRST-VALUE-SECRET" not in runtime_secrets.scrub("echo R7-FIRST-VALUE-SECRET")


@pytest.mark.parametrize("encode", [
    lambda v: v,
    lambda v: quote(v, safe=""),
    lambda v: quote(v),
    lambda v: quote_plus(v),
    lambda v: json.dumps(v)[1:-1],
    lambda v: base64.b64encode(v.encode()).decode(),
    lambda v: base64.b64encode(v.encode()).decode().rstrip("="),
    lambda v: base64.urlsafe_b64encode(v.encode()).decode(),
], ids=["raw", "quote", "quote with slash", "quote_plus", "json", "base64", "base64 unpadded", "urlsafe base64"])
def test_every_form_the_gateway_can_produce_is_removed(encode):
    log_scrub.clear()
    value = "pa ss/wo+rd&x=1\"é~"
    runtime_secrets.remember(value, "forms")
    assert runtime_secrets.scrub(f"echo {encode(value)} end", limit=1000) == "echo [removed] end"


def test_a_basic_authorization_header_is_removed():
    log_scrub.clear()
    credential = "service-user:s3cr3t-R7-password"
    runtime_secrets.remember(credential, "basic")
    header = "Basic " + base64.b64encode(credential.encode()).decode()
    assert runtime_secrets.scrub(f"the request carried Authorization: {header}", limit=1000) == "the request carried Authorization: Basic [removed]"


def test_a_line_is_scrubbed_before_it_is_cut_so_a_value_across_the_cut_is_not_half_shown():
    log_scrub.clear()
    runtime_secrets.remember("CROSSING-THE-CUT-VALUE", "cut")
    text = "x" * 290 + "CROSSING-THE-CUT-VALUE" + "y" * 100
    cleaned = runtime_secrets.scrub(text, limit=300)
    assert "CROSS" not in cleaned and cleaned.startswith("x" * 290 + "[removed]")


def test_a_value_too_long_to_register_makes_every_line_unloggable_until_its_source_is_forgotten():
    log_scrub.clear()
    assert runtime_secrets.remember("L" * (64 * 1024), "long") is True
    assert runtime_secrets.remember("L" * (64 * 1024 + 1), "too-long") is False
    assert runtime_secrets.scrub("any line at all") == log_scrub.WITHHELD
    # a shorter value under the same source does not make the longer one safe to log (decision D44)
    assert runtime_secrets.remember("short-enough-now", "too-long") is True
    assert runtime_secrets.scrub("any line at all") == log_scrub.WITHHELD
    log_scrub.forget("too-long")
    assert runtime_secrets.scrub("any line at all") == "any line at all"


def test_a_secret_that_is_deleted_is_forgotten_and_one_that_exists_is_not(store):
    log_scrub.clear()
    kept = secret_refs.store("R7-KEPT-SECRET-VALUE", db_path=store)
    gone = secret_refs.store("R7-GONE-SECRET-VALUE", db_path=store)
    runtime_secrets.resolve(kept)
    runtime_secrets.resolve(gone)
    secret_store.delete_provider_secret(gone, db_path=store)
    cleaned = runtime_secrets.scrub("R7-KEPT-SECRET-VALUE R7-GONE-SECRET-VALUE")
    assert cleaned == "[removed] R7-GONE-SECRET-VALUE"


def test_a_source_keeps_the_values_it_held_before_a_rotation():
    log_scrub.clear()
    for value in ("R7-ROTATION-ONE", "R7-ROTATION-TWO", "R7-ROTATION-THREE", "R7-ROTATION-FOUR"):
        runtime_secrets.remember(value, "env:ROTATING")
    cleaned = runtime_secrets.scrub("R7-ROTATION-ONE R7-ROTATION-TWO R7-ROTATION-THREE R7-ROTATION-FOUR")
    assert cleaned == "[removed] [removed] [removed] [removed]"


def test_a_value_is_registered_under_a_source_always():
    with pytest.raises(ValueError):
        log_scrub.remember("a value", "")


# ---------------------------------------------------------------------------
# MED 4: the readers the structural test found
# ---------------------------------------------------------------------------

def test_a_literal_downstream_secret_is_registered(monkeypatch):
    log_scrub.clear()
    assert proxy.resolve_secret_value("R7-LITERAL-DOWNSTREAM-SECRET") == "R7-LITERAL-DOWNSTREAM-SECRET"
    assert "R7-LITERAL-DOWNSTREAM-SECRET" not in runtime_secrets.scrub("echo R7-LITERAL-DOWNSTREAM-SECRET")
    log_scrub.clear()
    proxy.resolve_secret_value({"value": "R7-LITERAL-IN-A-MAPPING", "prefix": "Bearer"})
    assert "R7-LITERAL-IN-A-MAPPING" not in runtime_secrets.scrub("echo R7-LITERAL-IN-A-MAPPING")


def test_the_discovery_keys_are_registered(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL_DISCOVERY_API_KEY", "R7-DISCOVERY-A-VALUE")
    monkeypatch.setenv("OPENAI_API_KEY", "R7-DISCOVERY-B-VALUE")
    log_scrub.clear()

    async def refuse(self, request):
        return httpx.Response(401, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    asyncio.run(proxy.refresh_openai_model_aliases())
    cleaned = runtime_secrets.scrub("R7-DISCOVERY-A-VALUE R7-DISCOVERY-B-VALUE")
    assert cleaned == "[removed] [removed]"


# ---------------------------------------------------------------------------
# LOW 5: the destination is what the reader dials
# ---------------------------------------------------------------------------

def test_the_destination_shown_has_its_dot_segments_resolved_as_the_readers_resolve_them(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    held, refs = _unbound_plant(gateway, owner)
    _reaim(gateway, "/a/../ready")
    shown = {row["carrier"]: row["destination"] for row in _engine_view(owner)["url_credentials"]}
    assert shown["config.health_endpoint"] == f"{GOOD}/ready"
    raw = _confirm(owner, destination=f"{GOOD}/a/../ready")
    assert raw.status_code == 400 and error_code(raw) == "destination_changed", raw.text
    from gateway import record_carriers

    health = record_carriers.refs_in(held["config"]["health_endpoint_entries"])
    assert {secret_rows(gateway)[ref] for ref in health} == {None}
    done = _confirm(owner, destination=f"{GOOD}/ready")
    assert done.status_code == 200, done.text
    assert {secret_rows(gateway)[ref] for ref in health} == {GOOD}


@pytest.mark.parametrize("path,dialed", [
    ("/a/../ready", "/ready"), ("/a/./b", "/a/b"), ("/../x", "/x"), ("/ready", "/ready"),
])
def test_the_dialed_path_is_what_the_readers_and_the_http_client_request(path, dialed):
    from gateway import url_binding

    assert url_binding.dialed_path(path) == dialed
    assert httpx.URL(f"http://h.invalid:9{path}").path == dialed, "the same resolution the HTTP client applies"


def test_an_empty_segment_has_no_single_destination_because_urljoin_collapses_it_and_the_client_keeps_it():
    from gateway import url_binding

    assert url_binding.dialed_path("/a//b") is None


def test_a_path_that_ends_in_a_dot_segment_has_no_single_destination():
    from gateway import url_binding

    assert url_binding.dialed_path("/a/b/..") is None and url_binding.dialed_path("/a/.") is None
    assert url_binding.destination("/a/b/..", "http://good.invalid:9") is None
