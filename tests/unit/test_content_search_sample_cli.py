from __future__ import annotations

import json

from cognistore.samples.content_search.cli import main


def test_sample_cli_verifies_packaged_corpus(capsys) -> None:
    assert main(["verify"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "corpus": "cognistore-content-search-sample-v1",
        "duplicate_content_groups": 1,
        "license": "MIT",
        "objects": 4,
        "queries": ["ask", "keyword", "vector"],
        "schema_version": 1,
        "status": "verified",
    }


def test_sample_cli_reports_missing_runtime_configuration(capsys, monkeypatch) -> None:
    monkeypatch.delenv("COGNISTORE_DRIVERS", raising=False)
    monkeypatch.delenv("COGNISTORE_CATALOG_DB", raising=False)
    assert main(["load"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--drivers or COGNISTORE_DRIVERS is required" in captured.err


def test_sample_cli_validates_serve_port_before_opening_runtime(capsys) -> None:
    assert main(["serve", "--port", "0"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--port must be between 1 and 65535" in captured.err
