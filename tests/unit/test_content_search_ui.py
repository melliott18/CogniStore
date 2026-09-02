from __future__ import annotations

from fastapi.testclient import TestClient

from cognistore.api import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.core.catalog import Catalog


def _client() -> TestClient:
    return TestClient(
        create_app(CogniStoreGateway(Catalog(), {})),
        follow_redirects=False,
    )


def test_content_search_ui_is_served_from_same_origin_with_security_headers() -> None:
    with _client() as client:
        root = client.get("/")
        canonical = client.get("/ui")
        index = client.get("/ui/")
        script = client.get("/ui/app.js")
        styles = client.get("/ui/styles.css")

    assert root.status_code == canonical.status_code == 307
    assert root.headers["location"] == canonical.headers["location"] == "/ui/"
    assert index.status_code == script.status_code == styles.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert script.headers["content-type"].startswith("text/javascript")
    assert styles.headers["content-type"].startswith("text/css")
    for response in (root, canonical, index, script, styles):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert "script-src 'self'" in response.headers["content-security-policy"]


def test_ui_contract_covers_search_ask_filters_citations_and_explicit_states() -> None:
    with _client() as client:
        html = client.get("/ui/").text
        javascript = client.get("/ui/app.js").text

    for marker in (
        'data-view="search"',
        'data-view="ask"',
        'value="keyword"',
        'value="vector"',
        'id="filter-bucket"',
        'id="filter-document-metadata"',
        'id="results-list"',
    ):
        assert marker in html
    for behavior in (
        "retrieval_mode: retrievalMode",
        "showLoading()",
        "No matching content",
        "provider degraded",
        "showError(error.message",
        "Download cited object",
        "Inspect passage citation",
    ):
        assert behavior in javascript
    # Every server value is written through textContent rather than parsed as markup.
    assert ".innerHTML" not in javascript


def test_ui_routes_do_not_change_the_versioned_openapi_contract() -> None:
    app = create_app(CogniStoreGateway(Catalog(), {}))

    paths = app.openapi()["paths"]

    assert "/v1/ask" in paths
    assert "/" not in paths
    assert not any(path.startswith("/ui") for path in paths)
