"""Round 2 of the secret handling, the pure part: how a URL, a command, a header list and an environment are stored.

The cases come from the independent review of Phase 1 (``Documents/azizam/p1-rv-probes/test_rv3.py``): URLs whose query
string the first version rewrote, an ambiguous host, command lines the credential rule missed, and header and
environment shapes that stayed inline.
"""

from __future__ import annotations

import copy
import json

import pytest

from gateway import projection, record_carriers as rc, runtime_secrets, secret_refs
from gateway.legacy_credential_rules import argv_secret_slots


class Store:
    """An in-memory secret store: ``put`` hands out references, ``resolve`` reads them back."""

    def __init__(self, monkeypatch):
        self.values: dict[str, str] = {}
        monkeypatch.setattr(runtime_secrets, "resolve", lambda ref, *, origin=None: self.values[ref])

    def put(self, value, origin=None):
        ref = secret_refs.new_ref()
        self.values[ref] = value
        return ref


def _resolved(store, container, key):
    """The bytes the stored parts put back (decision D39 decides separately whether the origin lets them be sent)."""
    held = container.get(key + "_entries")
    if not held:
        return container[key]
    if held.get("whole"):
        return runtime_secrets.resolve(held["whole"]["secret_ref"])
    return runtime_secrets.restore(str(container.get(key) or ""), held, "http://test.invalid:80")


URLS = [
    "http://u:p@h/x", "http://u:p@h:80/x?a=b&c=d#f",
    "http://h/x?a=b+c", "http://h/x?a=%2f", "http://h/x?a=%2F", "http://h/x?a=", "http://h/x?a=b=c", "http://h/x?a&b", "http://h/x?a=b&&c=d",
    "http://h/x?a=b%20c", "http://h/x?a=%zz", "http://h/x?a=b;c=d", "http://h/x?token=é", "http://h/x?a=1&a=2", "http://h/x?=v",
    "http://h/x?%41=1", "http://h/x?a=b#", "http://h/x#frag", "http://h/x?", "http://h/x?#", "http://h/x?&", "http://h/x?a=b&", "http://h/x?&a=b",
    "http://u@h/", "http://:p@h/", "http://u:@h/", "http://u%40x:p%3A@h/", "http://a@b@h/",
    "http://u:p@[::1]:8080/v1", "http://[::1]:8080/v1?token=z",
    "HTTP://U:P@H/X?A=B", "https://u:p@h/x?a=b", "u:p@h:8000", "u:p@h", "h:8000/x?token=a", "/health?token=a", "/ready?sig=a&b=c",
    "health?token=a", "?token=a", "/x#frag", "http://h/x?a=b&token=%E2%9C%93", "http://h/%7Euser?a=b", "unix:///tmp/s.sock?token=a",
    "ftp://u:p@h/", "ws://u:p@h/x?a=b", "http://u:p@h?a=b", "http://h?a=b", "http://h#f", "http://u:p:q@h/", "http://u:p@h@i/",
    "http://h/x?token123", "http://h/x?a=b&token123&c=",
]


@pytest.mark.parametrize("url", URLS)
def test_a_url_is_stored_and_read_back_byte_for_byte(monkeypatch, url):
    store = Store(monkeypatch)
    container = {"base_url": url}
    rc.externalize_url(container, "base_url", store.put)
    assert _resolved(store, container, "base_url") == url
    # idempotent: a second pass changes nothing and still gives the same bytes
    again = copy.deepcopy(container)
    assert rc.externalize_url(again, "base_url", store.put) is False and again == container
    # the public field carries no credential: no user info, no query, no fragment
    shown = projection.public_url(container["base_url"])
    assert "@" not in shown.partition("//")[2].split("/")[0] and "?" not in shown and "#" not in shown


@pytest.mark.parametrize("url,query", [
    ("http://h/x?a=b+c", [("a", "b+c")]), ("http://h/x?a=", [("a", "")]), ("http://h/x?a=b&&c=d", [("a", "b"), ("", None), ("c", "d")]),
    ("http://h/x?a=%2f", [("a", "%2f")]), ("http://h/x?", [("", None)]), ("http://h/x?token123", [("token123", None)]),
])
def test_the_query_is_kept_as_raw_chunks(url, query):
    _public, parts = rc.split_url(url)
    assert parts["query"] == query


@pytest.mark.parametrize("url", ["http://evil.com\\@good.com/", "http://good.com\\@evil.com/", "http://h/x\\?a=b", "http://h\t/x", "http://h:99999/x?token=a"])
def test_an_ambiguous_url_is_refused_on_a_write_and_kept_whole_and_private_for_a_stored_record(monkeypatch, url):
    store = Store(monkeypatch)
    with pytest.raises(rc.AmbiguousUrl):
        rc.split_url(url)
    record = {"id": "e", "config": {"host": "10.0.0.5", "port": 1, "base_url": url}}
    with pytest.raises(rc.UrlRefused) as refused:
        rc.externalize_service(record, store.put, reject_ambiguous=True)
    assert url not in str(refused.value) and "config.base_url" in str(refused.value)
    stored, _ = rc.externalize_service(record, store.put)  # a record that exists already must not lose it
    assert stored["config"]["base_url"] == "" and _resolved(store, stored["config"], "base_url") == url


def test_the_backslash_form_is_not_read_as_the_host_it_names_in_the_public_field():
    # urlsplit and a browser disagree about this host; the first version stored the public form as good.com
    with pytest.raises(rc.AmbiguousUrl):
        rc.split_url("http://evil.com\\@good.com/")


@pytest.mark.parametrize("path", [
    "config.endpoint", "config.upstream", "lifecycle.driver_config.load_url", "lifecycle.driver_config.unload_url",
    "lifecycle.driver_config.verify_endpoint", "config.lifecycle.driver_config.load_url",
])
def test_a_url_field_outside_the_first_list_that_the_runtime_reads_is_a_url_carrier(monkeypatch, path):
    assert path in rc.URL_FIELD_PATHS
    store = Store(monkeypatch)
    record: dict = {"id": "e", "config": {"host": "10.0.0.5", "port": 1}}
    parent_path, _, key = path.rpartition(".")
    node = record
    for part in parent_path.split("."):
        node = node.setdefault(part, {})
    node[key] = "http://u:ZPW-URLFIELD@10.0.0.9:8000/x?token=ZTOK-URLFIELD"
    stored, moved = rc.externalize_service(record, store.put)
    assert path in moved and "ZPW-URLFIELD" not in json.dumps(stored) and "ZTOK-URLFIELD" not in json.dumps(stored)
    holder = stored
    for part in parent_path.split("."):
        holder = holder[part]
    assert _resolved(store, holder, key) == "http://u:ZPW-URLFIELD@10.0.0.9:8000/x?token=ZTOK-URLFIELD"


def test_a_model_row_url_is_a_url_carrier_and_is_shown_in_public_form(monkeypatch):
    store = Store(monkeypatch)
    record = {"id": "e", "config": {"host": "10.0.0.5", "port": 1, "models": [
        {"id": "a", "upstream_model": "a"}, {"id": "m", "upstream_model": "m", "url": "http://u:ZPW-MODEL@10.0.0.5/v1?key=ZTOK-MODEL"}]}}
    stored, moved = rc.externalize_service(record, store.put)
    assert "config.models[1].url" in moved and "ZPW-MODEL" not in json.dumps(stored) and "ZTOK-MODEL" not in json.dumps(stored)
    row = stored["config"]["models"][1]
    assert row["url"] == "http://10.0.0.5/v1" and _resolved(store, row, "url") == "http://u:ZPW-MODEL@10.0.0.5/v1?key=ZTOK-MODEL"
    shown = projection.project("ServiceRecord", {"id": "e", "name": "e", "kind": "engine", **stored}, configured=lambda ref: True).tree
    assert "ZPW-MODEL" not in json.dumps(shown) and shown["config"]["models"][1]["url"] == "http://10.0.0.5/v1"


# --- commands --------------------------------------------------------------------------------------------------------

SECRET_ARGV = [
    (["r", "--token", "S"], {2}), (["r", "--api-key=S"], {1}), (["r", "-pS"], {1}), (["r", "-p", "S"], {2}), (["r", "--password", "-S"], {2}),
    (["r", "--auth", "S"], {2}), (["r", "--hf-token", "S"], {2}), (["r", "--apiKey", "S"], {2}), (["r", "--API-KEY", "S"], {2}),
    # the values that follow a header or environment flag are secret by default, whatever the flag's own name
    (["r", "-H", "Authorization: Bearer S"], {2}), (["r", "--header", "X-Api-Key: S"], {2}), (["r", "-H", "X-Custom: S"], {2}),
    (["r", "--header=Authorization: Bearer S"], {1}), (["r", "-e", "FOO=S"], {2}), (["r", "--env", "FOO=S"], {2}), (["r", "--env=SECRET_X=S"], {1}),
    (["r", "-HX-A: S"], {1}),
    (["r", "--login", "S"], {2}), (["r", "--cred", "S"], {2}), (["r", "--creds", "S"], {2}), (["r", "--pw", "S"], {2}), (["r", "--tok", "S"], {2}),
    (["r", "--apitoken", "S"], {2}), (["r", "--oauth", "S"], {2}), (["r", "--sas", "S"], {2}), (["r", "--connection-string", "S"], {2}),
    (["r", "--conn", "S"], {2}), (["r", "--license", "S"], {2}), (["r", "--webhook", "https://hooks/x/S"], {2}), (["r", "--authkey", "S"], {2}),
    (["r", "--ngrok-authtoken", "S"], {2}), (["r", "--account", "S"], {2}), (["r", "--user", "u:S"], {2}), (["r", "-u", "u:S"], {2}),
    (["r", "--dsn", "postgres://u:S@h/db"], {2}), (["r", "--url", "http://h/x?k=S"], {2}), (["r", "AWS_SECRET_ACCESS_KEY=S"], {1}),
    (["r", "OPENAI_API_KEY", "S"], {2}), (["r", "--model-path", "/m", "--login", "S"], {4}),
]
# Values no name and no structure marks as a credential: they stay public, as the writer sent them.
PUBLIC_ARGV = [
    ["r", "--port", "8000"], ["r", "--tokenizer", "t"], ["r", "--max-tokens", "10"], ["r", "--keyfile", "/k"], ["r", "--model", "/models/x.gguf"],
    ["r", "--foo", "S"], ["r", "--foo=S"], ["r", "S"],
]


@pytest.mark.parametrize("argv,secret_indexes", SECRET_ARGV)
def test_the_value_of_a_credential_flag_header_or_env_flag_is_a_secret(argv, secret_indexes):
    slots = argv_secret_slots(argv)
    covered = {index for index, visible in slots.items() if visible < len(argv[index]) and "S" in argv[index][visible:]}
    assert secret_indexes <= covered, (argv, slots)


@pytest.mark.parametrize("argv", PUBLIC_ARGV)
def test_an_ordinary_argument_stays_public(argv):
    assert argv_secret_slots(argv) == {}


def test_a_line_that_cannot_be_split_is_stored_whole_with_a_public_label_and_never_shown(monkeypatch):
    store = Store(monkeypatch)
    record = {"id": "e", "process": {"driver": "process", "start": "runner --password 'ZUNBALANCED-1"}}
    stored, moved = rc.externalize_service(record, store.put)
    start = stored["process"]["start"]
    assert "process.start" in moved and "ZUNBALANCED-1" not in json.dumps(stored)
    assert len(start) == 1 and start[0]["label"] == rc.WHOLE_COMMAND_LABEL and secret_refs.is_secret_entry(start[0])
    shown = projection.project("ServiceRecord", {"id": "e", "name": "e", "kind": "engine", **stored}, configured=lambda ref: True).tree
    assert shown["process"]["start"] == [{"id": start[0]["id"], "label": rc.WHOLE_COMMAND_LABEL, "configured": True}]
    with pytest.raises(ValueError):  # run, it fails as the line always did
        runtime_secrets.argv(start)


def test_an_argument_list_with_a_number_after_a_credential_flag_is_stored_as_a_secret(monkeypatch):
    store = Store(monkeypatch)
    stored, moved = rc.externalize_service({"id": "e", "process": {"start": ["runner", "--pin", 424242424]}}, store.put)
    assert "424242424" not in json.dumps(stored) and runtime_secrets.argv(stored["process"]["start"]) == ["runner", "--pin", "424242424"]


def test_a_command_given_as_structured_items_is_not_guessed_at(monkeypatch):
    """A literal item of a structured command is public as declared: no flag name is read (decision D29)."""
    from gateway import secret_commands

    store = Store(monkeypatch)
    record = {"id": "e", "process": {"driver": "process", "start": ["runner"]}}
    command = {"carrier": "process.start", "operation": "set", "items": [{"literal": "runner"}, {"literal": "--token"}, {"literal": "not-secret-by-declaration"}], "clear": []}
    out, outcome = secret_commands.apply_commands(record, [command], store.put)
    assert out["process"]["start"] == ["runner", "--token", "not-secret-by-declaration"] and outcome.created == []


# --- headers and environments ---------------------------------------------------------------------------------------

def _headers(monkeypatch, headers):
    store = Store(monkeypatch)
    record = {"id": "e", "config": {"host": "10.0.0.5", "port": 1, "headers": copy.deepcopy(headers)}}
    stored, moved = rc.externalize_service(record, store.put)
    return store, stored["config"]["headers"], moved


@pytest.mark.parametrize("headers", [
    [{"name": "X", "value": "ZH-1", "public": "yes"}], [{"name": "X", "value": "ZH-1", "public": 1}], [{"name": "X", "value": "ZH-1", "public": "true"}],
    [{"name": "X", "value": "ZH-1", "secret_ref": "sref_" + "0" * 24}], [["X", "ZH-1"]], ["X: ZH-1"], [{"X": "ZH-1"}], "X: ZH-1",
    [{"name": 5, "value": "ZH-1"}], [{"value": "ZH-1"}], [{"name": "X", "value": "ZH-1", "extra": "ignored"}],
    {"X": "ZH-1"}, {"X": {"value": "ZH-1"}}, {"X": ["ZH-1"]},
])
def test_no_shape_of_a_header_list_stays_inline(monkeypatch, headers):
    store, stored, moved = _headers(monkeypatch, headers)
    assert "ZH-1" not in json.dumps(stored) and moved
    assert "ZH-1" in json.dumps(list(store.values.values()))


def test_only_a_row_that_says_public_true_stays_public_and_it_keeps_its_id(monkeypatch):
    _, stored, moved = _headers(monkeypatch, [{"id": "ent_" + "a" * 16, "name": "X-Region", "value": "eu", "public": True}])
    assert stored == [{"id": "ent_" + "a" * 16, "name": "X-Region", "value": "eu", "public": True}] and moved == []


def test_a_row_with_both_a_secret_ref_and_a_value_keeps_its_id_and_holds_only_a_new_reference(monkeypatch):
    old = "sref_" + "1" * 24
    _, stored, _ = _headers(monkeypatch, [{"id": "ent_" + "b" * 16, "name": "X", "secret_ref": old, "value": "ZH-2"}])
    assert len(stored) == 1 and stored[0]["id"] == "ent_" + "b" * 16 and stored[0]["secret_ref"] != old and "value" not in stored[0]


@pytest.mark.parametrize("env", [{"A": {"value": "ZE-1"}}, {"A": ["ZE-1"]}, {"A": "ZE-1"}, {"A": {"value": "ZE-1", "public": "yes"}},
                                 {"A": {"id": "ent_" + "c" * 16, "secret_ref": "sref_" + "2" * 24, "value": "ZE-1"}}])
def test_no_shape_of_an_environment_value_stays_inline(monkeypatch, env):
    store = Store(monkeypatch)
    stored, moved = rc.externalize_service({"id": "e", "process": {"driver": "process", "start": ["r"], "env": copy.deepcopy(env)}}, store.put)
    assert "ZE-1" not in json.dumps(stored) and moved


def test_a_public_environment_entry_stays_public_and_a_switch_or_empty_value_holds_nothing_to_move(monkeypatch):
    store = Store(monkeypatch)
    env = {"A": {"id": "ent_" + "d" * 16, "value": "eu", "public": True}, "B": True, "C": "", "D": None}
    stored, moved = rc.externalize_service({"id": "e", "process": {"driver": "process", "start": ["r"], "env": copy.deepcopy(env)}}, store.put)
    assert stored["process"]["env"] == env and moved == []
