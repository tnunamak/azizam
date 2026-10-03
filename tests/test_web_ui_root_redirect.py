from fastapi.testclient import TestClient

import proxy


def test_ui_without_trailing_slash_redirects_to_ui_root_and_keeps_query():
    client = TestClient(proxy.app)
    response = client.get("/ui?x=1", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/ui/?x=1"
