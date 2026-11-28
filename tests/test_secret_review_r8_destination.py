"""Round 8 of the secret handling, the destination the owner confirms: what the review of phase1-r7 (6d7f21d4) found (D44).

* MED 7: ``url_binding.destination`` resolved the dot segments of the field's path without the base path. With the base
  ``http://good.invalid:9/prefix`` and the field ``/a/../ready`` the owner was shown ``/ready`` and the reader dialed
  ``/prefix/ready``.

The destination is the one the reader's own join gives, base path included; where the readers of a field do not agree on it
there is none to show.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
import pytest

from gateway import engine_observations, url_binding
from gateway.engine_check import join_url

BASE = "http://good.invalid:9/prefix"


def test_the_reviewers_repro_the_base_path_is_part_of_the_destination():
    assert url_binding.destination("/a/../ready", BASE) == "http://good.invalid:9/prefix/ready"


@pytest.mark.parametrize("base,path,dialed", [
    ("http://good.invalid:9/prefix", "/a/../ready", "/prefix/ready"),
    ("http://good.invalid:9/prefix/", "/a/./b", "/prefix/a/b"),
    ("http://good.invalid:9/deep/er", "/../x", "/deep/x"),
    ("http://good.invalid:9", "/a/../ready", "/ready"),
    ("http://good.invalid:9/v1", "/ready", "/v1/ready"),
])
def test_the_destination_is_what_the_observation_reader_dials(base, path, dialed):
    reader_path = urlsplit(engine_observations._join_path(base, path)).path
    client_path = httpx.URL(engine_observations._join_path(base, path)).path
    assert reader_path == dialed == client_path
    assert url_binding.destination(path, base) == f"http://good.invalid:9{dialed}"


@pytest.mark.parametrize("base,path", [
    ("http://good.invalid:9/prefix", "/a/../ready"),
    ("http://good.invalid:9/v1", "/ready"),
    ("http://good.invalid:9", "/x/./y"),
])
def test_the_destination_is_what_a_concatenating_reader_dials(base, path):
    dialed = httpx.URL(join_url(base, path)).path
    assert url_binding.destination(path, base) == f"http://good.invalid:9{dialed}"


def test_the_destination_of_an_observation_field_joins_the_connection_address():
    record = {"id": "e", "config": {"connection": {"address": BASE}, "observations": {"selected": {"op": "selected", "http": {"path": "/a/../ready"}}}}}
    path = "observations.selected.http.path"
    assert url_binding.base_of(record, path) == BASE
    assert url_binding.destination_of(record, path, "/a/../ready") == "http://good.invalid:9/prefix/ready"


def test_readers_that_dial_the_root_and_readers_that_keep_the_prefix_have_no_single_destination():
    # a record with a host and port and an explicit base URL: some health readers dial the root, others the base path
    record = {"id": "e", "config": {"host": "good.invalid", "port": 9, "base_url": BASE, "health_endpoint": "/ready"}}
    assert url_binding.destination_of(record, "config.health_endpoint", "/ready") is None
    # the same record without the prefix: both readers dial the same address
    plain = {"id": "e", "config": {"host": "good.invalid", "port": 9, "base_url": "http://good.invalid:9", "health_endpoint": "/ready"}}
    assert url_binding.destination_of(plain, "config.health_endpoint", "/ready") == "http://good.invalid:9/ready"
    # a host and port alone: every reader dials the root, the default /v1 base path is not part of a health address
    hostport = {"id": "e", "config": {"host": "good.invalid", "port": 9, "health_endpoint": "/ready"}}
    assert url_binding.destination_of(hostport, "config.health_endpoint", "/ready") == "http://good.invalid:9/ready"


def test_an_explicit_base_alone_gives_the_path_every_reader_keeps():
    record = {"id": "e", "config": {"base_url": BASE, "health_endpoint": "/a/../ready"}}
    assert url_binding.destination_of(record, "config.health_endpoint", "/a/../ready") == "http://good.invalid:9/prefix/ready"


def test_an_absolute_field_has_no_base():
    assert url_binding.destination("http://other.invalid:5/x/../y", BASE) == "http://other.invalid:5/y"
