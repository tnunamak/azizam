"""Router-backed contracts for extension-catalog path parameter grammar."""

import asyncio
from pathlib import Path

import pytest
import yaml
from starlette.convertors import CONVERTOR_TYPES, Convertor, register_url_convertor
from starlette.responses import PlainTextResponse
from starlette.routing import Match, Route, Router

from gateway import extension_catalog as catalog


ROWS = Path(__file__).resolve().parents[1] / "registry" / "protocol_frontends.yml"


def write_rows(tmp_path, paths):
    path = tmp_path / "converter-rows.yml"
    rows = [
        {"canonical": route, "extensions": ["com.azizam.chat"], "extension_order": order}
        for order, route in enumerate(paths, start=1)
    ]
    path.write_text(yaml.safe_dump({"records": [{"registered_routes": rows}]}), encoding="utf-8")
    return path


def request_scope(path):
    return {
        "type": "http", "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": path, "root_path": "", "query_string": b"",
        "headers": [], "server": ("testserver", 80), "client": ("testclient", 123),
    }


def route_endpoint(label):
    async def endpoint(request):
        return PlainTextResponse(label)
    return endpoint


async def dispatch(router, path):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await router(request_scope(path), receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return status, body.decode()


@pytest.mark.parametrize("paths, requests", [
    (
        ("/v1/items/{a:int }", "/v1/items/{b:int }"),
        ("/v1/items/{a:int }", "/v1/items/{b:int }"),
    ),
    (
        ("/v1/items/{a :int}", "/v1/items/{b:int}"),
        ("/v1/items/{a :int}", "/v1/items/42"),
    ),
], ids=["space-in-converter-is-literal", "space-in-name-is-literal"])
def test_whitespace_braces_keep_router_identity(tmp_path, paths, requests):
    index = catalog.load_extension_routes(write_rows(tmp_path, paths))
    assert [row.route for row in index["com.azizam.chat"]] == list(paths)
    router = Router(routes=[
        Route(row.route, route_endpoint(str(index)), methods=["GET"])
        for index, row in enumerate(index["com.azizam.chat"])
    ])
    for expected, request_path in enumerate(requests):
        assert [
            route.matches(request_scope(request_path))[0] for route in router.routes
        ] == [Match.FULL if index == expected else Match.NONE for index in range(2)]
        assert asyncio.run(dispatch(router, request_path)) == (200, str(expected))


def test_unknown_converter_is_refused_with_file_and_row(tmp_path):
    path = write_rows(tmp_path, ["/valid", "/v1/items/{item:catalog_bogus}"])
    assert "catalog_bogus" not in CONVERTOR_TYPES
    with pytest.raises(ValueError) as error:
        catalog.load_extension_routes(path)
    assert str(error.value) == (
        f"{path}: record 1, row 2: unknown path converter 'catalog_bogus': "
        "'/v1/items/{item:catalog_bogus}'"
    )


def test_registered_custom_converter_loads_and_matches(tmp_path):
    class TaggedInteger(Convertor):
        regex = r"tag-[0-9]+"

        def convert(self, value):
            return int(value.removeprefix("tag-"))

        def to_string(self, value):
            return f"tag-{value}"

    name = "catalog_test_integer"
    missing = object()
    previous = CONVERTOR_TYPES.get(name, missing)
    register_url_convertor(name, TaggedInteger())
    try:
        route_path = f"/v1/items/{{item:{name}}}"
        index = catalog.load_extension_routes(write_rows(tmp_path, [route_path]))
        assert [row.route for row in index["com.azizam.chat"]] == [route_path]
        router = Router(routes=[Route(route_path, route_endpoint("custom"), methods=["GET"])])
        match, child = router.routes[0].matches(request_scope("/v1/items/tag-42"))
        assert match == Match.FULL
        assert child["path_params"] == {"item": 42}
        assert router.routes[0].matches(request_scope("/v1/items/42"))[0] == Match.NONE
        assert asyncio.run(dispatch(router, "/v1/items/tag-42")) == (200, "custom")
    finally:
        if previous is missing:
            CONVERTOR_TYPES.pop(name, None)
        else:
            register_url_convertor(name, previous)


def test_duplicate_error_names_file_and_second_row(tmp_path):
    path = write_rows(tmp_path, ["/v1/items/{a:int}", "/v1/items/{b:int}"])
    with pytest.raises(ValueError) as error:
        catalog.load_extension_routes(path)
    assert str(error.value) == (
        f"{path}: record 1, row 2: duplicate extension route: "
        "namespace 'com.azizam.chat', route '/v1/items/{b:int}'"
    )


def test_empty_segment_error_names_file_and_second_row(tmp_path):
    path = write_rows(tmp_path, ["/valid", "/v1//items"])
    with pytest.raises(ValueError) as error:
        catalog.load_extension_routes(path)
    assert str(error.value) == (
        f"{path}: record 1, row 2: extension route has an empty path segment: '/v1//items'"
    )


def test_shipped_extension_counts_do_not_change():
    index = catalog.load_extension_routes(ROWS)
    assert {namespace: len(rows) for namespace, rows in index.items()} == {
        "com.azizam.prompts": 9,
        "com.azizam.workflow": 4,
        "com.azizam.chat": 2,
        "com.azizam.routing": 2,
    }
