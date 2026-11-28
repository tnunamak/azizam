"""The image count ceiling has one owner-editable value in workflow_execution."""

from __future__ import annotations

import proxy
from tests.support.config_snapshot import publish_section


def test_one_config_value_drives_admission_frontends_make_and_openapi(monkeypatch):
    publish_section(monkeypatch, "workflow_execution", {"max_images_per_request": 3})

    assert proxy.workflow_run.max_images_per_request(proxy.runtime().config) == 3
    assert proxy._workflow_run_core()._max_images() == 3
    assert proxy._session_make_limits().max_images == 3
    # The A1111 translator receives this same live callback at mount time.
    assert proxy._max_images_per_request() == 3

    proxy.app.openapi_schema = None
    schema = proxy.app.openapi()
    request_schema = schema["paths"]["/v1/images/generations"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["properties"]["n"]["maximum"] == 3
