from fastapi.testclient import TestClient

import proxy


def test_ui_without_trailing_slash_redirects_to_ui_root_and_keeps_query():
    client = TestClient(proxy.app)
    response = client.get("/ui?x=1", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/ui/?x=1"


def test_ui_page_without_trailing_slash_redirects_relatively():
    client = TestClient(proxy.app)
    response = client.get("/ui/home?tab=1", follow_redirects=False, headers={"x-forwarded-proto": "https"})
    assert response.status_code == 308
    assert response.headers["location"] == "/ui/home/?tab=1"


def test_ui_asset_paths_are_not_redirected():
    client = TestClient(proxy.app)
    response = client.get("/ui/_next/static/missing.js", follow_redirects=False)
    assert response.status_code != 308
