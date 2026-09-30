import hashlib
import json

import pytest

from scripts import load_acceptance as acceptance
from scripts import load_export as export
from scripts import load_qualification as base


@pytest.fixture
def spool(tmp_path):
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    campaign = {"run_id": "local-import-fixture", "bindings": {"revision": "fixture"}}
    (output / "campaign.json").write_text(json.dumps(campaign))
    identity = {"run_id": campaign["run_id"], "binding_sha256": acceptance.binding_digest(campaign)}
    for name in export.FILES:
        (source / name).write_text(json.dumps({**identity, "content": identity}) + "\n")
    return source, output, identity


def test_import_preserves_exact_bytes_and_hashes_without_asserting_acceptance(spool):
    source, output, identity = spool
    result = export.import_evidence(source, output, **identity)
    assert result["status"] == "imported-unreviewed"
    for name in export.FILES:
        assert (source / name).read_bytes() == (output / name).read_bytes()
        assert result["artifacts"][name] == hashlib.sha256((source / name).read_bytes()).hexdigest()
    # Identity validation is not semantic evaluation and cannot confer a pass.
    assert acceptance.evaluate(output)["status"] != "passed"


@pytest.mark.parametrize("failure", ["wrong-run", "wrong-source", "missing", "symlink", "duplicate-key", "nan", "empty", "oversized"])
def test_bad_evidence_never_partially_publishes(spool, failure):
    source, output, identity = spool
    path = source / "observed-artifacts.jsonl"
    if failure == "wrong-run":
        path.write_text(json.dumps({**identity, "run_id": "another-run"}))
    elif failure == "wrong-source":
        path.write_text(json.dumps({**identity, "content": {**identity, "run_id": "another-run"}}))
    elif failure == "missing":
        path.unlink()
    elif failure == "symlink":
        path.unlink()
        path.symlink_to(source / "jobs.jsonl")
    elif failure == "duplicate-key":
        path.write_text('{"run_id":"one","run_id":"two"}')
    elif failure == "nan":
        path.write_text('{"value":NaN}')
    elif failure == "empty":
        path.write_text("")
    else:
        path.write_text(" " * (export.MAX_ROW + 1))
    with pytest.raises(base.InvalidEvidence):
        export.import_evidence(source, output, **identity)
    assert {p.name for p in output.iterdir()} == {"campaign.json"}


def test_existing_evidence_is_never_overwritten(spool):
    source, output, identity = spool
    (output / "jobs.jsonl").write_text("retain this original")
    with pytest.raises(base.InvalidEvidence, match="export_output_exists"):
        export.import_evidence(source, output, **identity)
    assert (output / "jobs.jsonl").read_text() == "retain this original"


@pytest.mark.parametrize("extra", [0, 1])
def test_jsonl_limit_matches_acceptance_reader(spool, extra):
    source, output, identity = spool
    row = {**identity, "padding": ""}
    raw = json.dumps(row) + "\n"
    row["padding"] = "x" * (export.MAX_JSONL_ROW - len(raw) + extra)
    (source / "jobs.jsonl").write_text(json.dumps(row) + "\n")
    if extra:
        with pytest.raises(base.InvalidEvidence, match="export_record_too_large"):
            export.import_evidence(source, output, **identity)
    else:
        export.import_evidence(source, output, **identity)
