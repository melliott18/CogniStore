"""Offline synthetic corpus contracts; these tests do not qualify a deployment."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import zipfile
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest
from docx import Document
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("load_corpus", ROOT / "scripts/load_corpus.py")
assert SPEC is not None and SPEC.loader is not None
corpus = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = corpus
SPEC.loader.exec_module(corpus)


@pytest.mark.parametrize("count", [1000, 2000, 3000, 100000, 200000])
def test_exact_size_mime_joint_counts_and_tenant_balance(count: int) -> None:
    joint: Counter = Counter()
    tenant_size: Counter = Counter()
    tenant_mime: Counter = Counter()
    logical_bytes = 0
    for spec in corpus.iter_object_specs(count):
        joint[spec.size_bytes, spec.content_type] += 1
        tenant_size[spec.tenant, spec.size_bytes] += 1
        tenant_mime[spec.tenant, spec.content_type] += 1
        logical_bytes += spec.size_bytes
    assert sum(joint.values()) == count
    assert logical_bytes == 28426240000 * count // 100000
    for size, size_percentage in corpus.SIZE_PERCENTAGES:
        for mime, mime_percentage in corpus.MIME_PERCENTAGES:
            assert joint[size, mime] == count * size_percentage * mime_percentage // 10000
        for tenant in corpus.TENANTS:
            assert tenant_size[tenant, size] == count * size_percentage // 200
    for tenant in corpus.TENANTS:
        for mime, percentage in corpus.MIME_PERCENTAGES:
            assert tenant_mime[tenant, mime] == count * percentage // 200


def test_logical_keys_match_between_tenants_and_never_repeat() -> None:
    by_tenant = {tenant: {} for tenant in corpus.TENANTS}
    for spec in corpus.iter_object_specs(1000):
        assert spec.key not in by_tenant[spec.tenant]
        by_tenant[spec.tenant][spec.key] = spec.size_bytes
    assert by_tenant["pilot-a"] == by_tenant["pilot-b"]
    assert len(by_tenant["pilot-a"]) == 500


@pytest.mark.parametrize("size", [4096, 65536, 1048576, 16777216])
@pytest.mark.parametrize("mime", [corpus.OPAQUE, corpus.PDF, corpus.DOCX])
def test_valid_exact_size_documents_and_tenant_distinct_payloads(size: int, mime: str) -> None:
    spec = corpus.ObjectSpec("pilot-a", "corpus/00000000", size, mime)
    payload = corpus.generate_payload(spec, seed="document-test")
    assert len(payload) == size
    digest = hashlib.sha256(payload).hexdigest()
    assert digest == hashlib.sha256(corpus.generate_payload(spec, "document-test")).hexdigest()
    assert digest != hashlib.sha256(corpus.generate_payload(spec, "different-seed")).hexdigest()
    assert digest != hashlib.sha256(corpus.generate_payload(
        replace(spec, tenant="pilot-b"), "document-test"
    )).hexdigest()
    expected_text = corpus.payload_text(spec, "document-test")
    if mime == corpus.PDF:
        reader = PdfReader(io.BytesIO(payload), strict=True)
        assert not reader.is_encrypted
        assert len(reader.pages) == 1
        assert reader.pages[0].extract_text().strip() == expected_text
    elif mime == corpus.DOCX:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            assert archive.testzip() is None
            assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        document = Document(io.BytesIO(payload))
        assert [paragraph.text for paragraph in document.paragraphs] == [expected_text]
    else:
        assert payload.startswith(b"\x00CogniStore synthetic opaque\x00")
    assert len(expected_text.encode()) < 4 * 1024 * 1024


def test_manifest_generation_is_reproducible_and_writes_no_payloads_by_default(tmp_path: Path) -> None:
    first = corpus.build_corpus(tmp_path / "first", object_count=1000, seed="manifest-test")
    second = corpus.build_corpus(tmp_path / "second", object_count=1000, seed="manifest-test")
    assert first == second
    data = (tmp_path / "first/manifest.jsonl").read_bytes()
    assert data == (tmp_path / "second/manifest.jsonl").read_bytes()
    assert hashlib.sha256(data).hexdigest() == first["manifest_sha256"]
    assert first["logical_payload_bytes"] == 284262400
    assert first["tenant_object_counts"] == {"pilot-a": 500, "pilot-b": 500}
    assert first["tenant_logical_bytes"] == {"pilot-a": 142131200, "pilot-b": 142131200}
    assert first["payloads_written"] is False
    assert "no deployment qualification" in first["qualification_status"]
    assert not (tmp_path / "first/payloads").exists()
    rows = [json.loads(line) for line in data.splitlines()]
    assert len(rows) == 1000
    for row in (rows[0], rows[450], rows[-1]):
        spec = corpus.ObjectSpec(**{key: row[key] for key in (
            "tenant", "key", "size_bytes", "content_type"
        )})
        assert row["payload_path"] is None
        assert row["sha256"] == hashlib.sha256(
            corpus.generate_payload(spec, "manifest-test")
        ).hexdigest()
    assert json.loads((tmp_path / "first/summary.json").read_text()) == first


def test_payload_materialization_is_opt_in_and_existing_runs_are_preserved(tmp_path: Path) -> None:
    output = tmp_path / "payload-run"
    summary = corpus.build_corpus(output, object_count=1000, write_payloads=True)
    assert summary["payloads_written"] is True
    with (output / "manifest.jsonl").open() as manifest:
        for line in manifest:
            row = json.loads(line)
            payload = (output / row["payload_path"]).read_bytes()
            assert len(payload) == row["size_bytes"]
            assert hashlib.sha256(payload).hexdigest() == row["sha256"]
    before = (output / "summary.json").read_bytes()
    with pytest.raises(FileExistsError):
        corpus.build_corpus(output, object_count=1000, seed="changed")
    assert (output / "summary.json").read_bytes() == before


@pytest.mark.parametrize("count", [0, -1000, 1, 999, 1500])
def test_rejects_inexact_counts_before_creating_output(tmp_path: Path, count: int) -> None:
    with pytest.raises(ValueError, match="multiple"):
        corpus.build_corpus(tmp_path / "invalid", object_count=count)
    assert not (tmp_path / "invalid").exists()


def test_partial_run_has_no_completion_summary_and_cannot_be_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_generator(*args, **kwargs):
        raise RuntimeError("synthetic generator failure")

    monkeypatch.setattr(corpus, "generate_payload", broken_generator)
    output = tmp_path / "failed-run"
    with pytest.raises(RuntimeError, match="synthetic generator failure"):
        corpus.build_corpus(output, object_count=1000)
    assert not (output / "summary.json").exists()
    with pytest.raises(FileExistsError):
        corpus.build_corpus(output, object_count=1000)


def test_cli_help_and_invalid_arguments(capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        corpus.main(["--help"])
    assert exc.value.code == 0
    assert "--write-payloads" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        corpus.main(["--output", str(tmp_path / "bad"), "--objects", "3"])
    assert exc.value.code == 2
    assert "multiple of 1,000" in capsys.readouterr().err


def test_seed_and_external_descriptor_validation(tmp_path: Path) -> None:
    for seed in ("", "x" * 1025):
        with pytest.raises(ValueError, match="seed"):
            corpus.build_corpus(tmp_path / "invalid-seed", object_count=1000, seed=seed)
    spec = corpus.ObjectSpec("pilot-a", "corpus/00000000", 4096, corpus.PDF)
    for invalid in (
        replace(spec, tenant="production"), replace(spec, size_bytes=4097),
        replace(spec, content_type="text/plain"), replace(spec, key="a)<tag>"),
    ):
        with pytest.raises(ValueError):
            corpus.generate_payload(invalid)
