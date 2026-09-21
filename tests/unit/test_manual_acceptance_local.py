"""Safety boundaries for the explicitly synthetic localhost acceptance fixture."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.manual_acceptance_local import build_app, exercise, fixture_body


def test_fixture_roles_and_explicit_headers_override_browser_session(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "development")
    app = build_app(tmp_path)
    tokens_path = tmp_path / "tokens.private.json"
    assert tokens_path.stat().st_mode & 0o777 == 0o600
    tokens = json.loads(tokens_path.read_text())
    inventory = json.loads((tmp_path / "fixture.json").read_text())["provider_inventory"]
    assert inventory["metadata"] == "CatalogMetadataRetriever"
    assert all(inventory[name] is False for name in (
        "keyword_configured", "vector_configured", "answer_configured", "policy_embedding_configured",
    ))
    with TestClient(app) as client:
        assert client.get("/v1/catalog/objects?bucket=documents").status_code == 401
        client.get("/local-session/pilot-a-admin")
        response = client.put("/v1/objects/hot/documents/example.txt", content=b"synthetic")
        assert response.status_code == 201
        # Explicit reader credentials cannot acquire the selected admin cookie's grants.
        response = client.put("/v1/objects/hot/documents/example.txt", content=b"changed", headers={
            "Authorization": "Bearer " + tokens["pilot-a-reader"],
        })
        assert response.status_code == 403
        client.get("/local-session/pilot-b-reader")
        assert client.get("/v1/objects/hot/documents/example.txt").status_code == 404
        client.get("/local-session/sign-out")
        assert client.get("/v1/catalog/objects?bucket=documents").status_code == 401


def test_serve_refuses_existing_state_and_symlink(tmp_path):
    root = tmp_path / "existing"
    root.mkdir()
    marker = root / "original"
    marker.write_text("keep")
    with pytest.raises(ValueError, match="fresh empty"):
        build_app(root)
    assert marker.read_text() == "keep"
    link = tmp_path / "alias"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        build_app(link)


def test_failed_exercise_retains_every_attempt(tmp_path, monkeypatch):
    """A connectivity failure produces a distinct retained run on every invocation."""
    (tmp_path / "tokens.private.json").write_text(json.dumps({"pilot-a-writer": "fixture"}))
    (tmp_path / "fixture.json").write_text("{}")

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("synthetic transport unavailable")

    monkeypatch.setattr("httpx.Client.request", unavailable)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="synthetic transport unavailable"):
            exercise(tmp_path, 8763)
    retained = sorted((tmp_path / "runs").glob("*/rehearsal.json"))
    assert len(retained) == 2
    documents = [json.loads(path.read_text()) for path in retained]
    assert documents[0]["run_id"] != documents[1]["run_id"]
    assert all(document["failure"]["type"] == "RuntimeError" for document in documents)
    assert fixture_body("pilot-a", "API", 1) != fixture_body("pilot-b", "API", 1)


@pytest.mark.parametrize("arguments", [["--help"], ["serve"], ["exercise"]])
def test_optimized_python_cannot_skip_acceptance_assertions(tmp_path, arguments):
    script = Path(__file__).resolve().parents[2] / "scripts/manual_acceptance_local.py"
    root = tmp_path / "must-not-be-created"
    completed = subprocess.run(
        [sys.executable, "-O", "-m", "scripts.manual_acceptance_local",
         *arguments, "--root", str(root)],
        cwd=script.parent.parent, capture_output=True, text=True, check=False,
    )
    assert completed.returncode != 0
    assert "Acceptance assertions require Python without -O or PYTHONOPTIMIZE" in completed.stderr
    assert not root.exists()
