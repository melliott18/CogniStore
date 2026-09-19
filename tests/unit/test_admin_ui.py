from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cognistore.api import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.core.catalog import Catalog


def test_admin_shell_and_assets_are_same_origin_and_hardened() -> None:
    app = create_app(CogniStoreGateway(Catalog(), {}))
    with TestClient(app, follow_redirects=False) as client:
        redirect = client.get("/ui/admin")
        assert redirect.status_code == 307
        assert redirect.headers["location"] == "/ui/admin/"
        for path, content_type in (
            ("/ui/admin/", "text/html"),
            ("/ui/admin.js", "text/javascript"),
            ("/ui/admin.css", "text/css"),
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(content_type)
            assert "script-src 'self'" in response.headers["content-security-policy"]
            assert response.headers["x-frame-options"] == "DENY"
        assert client.get("/ui/admin/").headers["cache-control"] == "no-store"
        assert 'href="/ui/admin/"' in client.get("/ui/").text
        assert ".innerHTML" not in client.get("/ui/admin.js").text
    assert not any(path.startswith("/ui/") for path in app.openapi()["paths"])


def test_admin_ui_executable_behavior() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for administration UI behavior tests")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("admin_ui_behavior.js"))],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Administration UI behavior passed" in result.stdout
