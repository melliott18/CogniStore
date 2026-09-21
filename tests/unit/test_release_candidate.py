"""Candidate evidence must bind retained bytes and reject ambiguous source state."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def assembler():
    path = Path(__file__).resolve().parents[2] / "scripts/release_candidate.py"
    spec = importlib.util.spec_from_file_location("release_candidate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wheel(directory: Path, filename: str, name: str, version: str = "1.2.3") -> Path:
    path = directory / filename
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "distribution.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n",
        )
    return path


def test_lock_binds_canonical_distribution_versions_to_retained_bytes(assembler, tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    last = _wheel(wheels, "last.whl", "Zeta", "2.0rc1")
    first = _wheel(wheels, "first.whl", "Acme_Widget.Extras")
    output = tmp_path / "runtime.lock"

    assembler.wheel_lock(wheels, output)

    assert output.read_text().splitlines() == [
        "acme-widget-extras==1.2.3 --hash=sha256:" + hashlib.sha256(first.read_bytes()).hexdigest(),
        "zeta==2.0rc1 --hash=sha256:" + hashlib.sha256(last.read_bytes()).hexdigest(),
    ]


def test_lock_rejects_duplicate_normalized_distribution_names(assembler, tmp_path):
    _wheel(tmp_path, "one.whl", "Acme_Widget")
    _wheel(tmp_path, "two.whl", "acme.widget", "2.0")

    with pytest.raises(ValueError, match="Duplicate distribution"):
        assembler.wheel_lock(tmp_path, tmp_path.parent / "duplicate.lock")


def test_lock_uses_distribution_metadata_not_vendored_packages(assembler, tmp_path):
    wheel = _wheel(tmp_path, "setuptools.whl", "setuptools", "84.0.0")
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("setuptools/_vendor/example.dist-info/METADATA",
                         "Name: vendored-example\nVersion: 1.0\n")
    output = tmp_path.parent / "vendored.lock"
    assembler.wheel_lock(tmp_path, output)
    assert output.read_text().startswith("setuptools==84.0.0 --hash=sha256:")


def test_bootstrap_lock_selects_the_same_hashed_runtime_installer(assembler, tmp_path):
    pip = _wheel(tmp_path, "pip.whl", "pip", "26.2.1")
    _wheel(tmp_path, "runtime.whl", "example")
    output = tmp_path.parent / "bootstrap.lock"
    assembler.wheel_lock(tmp_path, output, only_package="pip")
    assert output.read_text() == "pip==26.2.1 --hash=sha256:" + assembler.digest(pip) + "\n"
    with pytest.raises(ValueError, match="empty wheelhouse"):
        assembler.wheel_lock(tmp_path, output, only_package="missing")


@pytest.mark.parametrize("invalid", ["empty", "source-archive", "symlink", "metadata", "name"])
def test_lock_rejects_unfreezable_artifacts(assembler, tmp_path, invalid):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    if invalid == "source-archive":
        (wheels / "package.tar.gz").write_bytes(b"not a retained binary wheel")
    elif invalid == "symlink":
        outside = _wheel(tmp_path, "outside.whl", "example")
        (wheels / "linked.whl").symlink_to(outside)
    elif invalid == "metadata":
        with zipfile.ZipFile(wheels / "empty.whl", "w") as archive:
            archive.writestr("unrelated.txt", "no package identity")
    elif invalid == "name":
        _wheel(wheels, "unsafe.whl", "example --extra-index-url=https://invalid.example")

    with pytest.raises(ValueError):
        assembler.wheel_lock(wheels, tmp_path / "runtime.lock")


@pytest.fixture
def bundle(assembler, tmp_path):
    directory = tmp_path / "candidate"
    directory.mkdir()
    for name in ("image.tar", "source.tar", "runtime.lock", "runtime.txt"):
        (directory / name).write_bytes((name + " retained bytes\n").encode())
    (directory / "reports").mkdir()
    (directory / "reports/smoke.json").write_text('{"passed": true}\n')
    assembler.write_json(directory / "manifest.json", {
        "status": "assembled-unqualified",
        "artifacts": assembler.bundle_files(directory),
    })
    assembler.seal(directory)
    return directory


def test_complete_bundle_verifies_without_claiming_qualification(assembler, bundle):
    assembler.verify(bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["status"] == "assembled-unqualified"


@pytest.mark.parametrize("change", ["edit", "remove", "add", "nested-checksums"])
def test_verify_rejects_changed_missing_or_unrecorded_files(assembler, bundle, change):
    if change == "edit":
        (bundle / "image.tar").write_bytes(b"different image")
    elif change == "remove":
        (bundle / "runtime.lock").unlink()
    elif change == "add":
        (bundle / "unrecorded.whl").write_bytes(b"new dependency")
    else:
        (bundle / "reports/SHA256SUMS").write_text("unrecorded evidence\n")

    with pytest.raises(ValueError, match="incomplete, changed, or contains unrecorded"):
        assembler.verify(bundle)


def test_resealing_checksums_does_not_remove_manifest_artifact_binding(assembler, bundle):
    (bundle / "image.tar").write_bytes(b"replacement image")
    assembler.seal(bundle)

    with pytest.raises(ValueError, match="Manifest artifact mismatch: image.tar"):
        assembler.verify(bundle)


def test_manifest_cannot_omit_required_installation_artifact(assembler, bundle):
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["artifacts"]["runtime.lock"]
    assembler.write_json(manifest_path, manifest)
    assembler.seal(bundle)

    with pytest.raises(ValueError, match="Missing required artifact: runtime.lock"):
        assembler.verify(bundle)


@pytest.mark.parametrize("path", ["../outside", "/absolute", "image.tar\nsecond-record"])
def test_verify_rejects_unsafe_checksum_records(assembler, bundle, path):
    (bundle / "SHA256SUMS").write_text("a" * 64 + "  " + path + "\n")

    with pytest.raises(ValueError):
        assembler.verify(bundle)


def test_verify_rejects_duplicate_checksum_records(assembler, bundle):
    path = bundle / "SHA256SUMS"
    contents = path.read_text()
    path.write_text(contents + contents.splitlines()[0] + "\n")

    with pytest.raises(ValueError, match="duplicate checksum path"):
        assembler.verify(bundle)


def test_bundle_cannot_follow_links_to_mutable_external_evidence(assembler, bundle, tmp_path):
    outside = tmp_path / "external-report.json"
    outside.write_text('{"passed": true}\n')
    (bundle / "reports/external.json").symlink_to(outside)

    with pytest.raises(ValueError, match="symlinks are forbidden"):
        assembler.seal(bundle)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.check_output([
        "git", "-c", "user.name=Candidate Test", "-c", "user.email=candidate@example.test",
        *arguments,
    ], cwd=root, text=True).strip()


@pytest.fixture
def source_checkout(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "--quiet")
    (root / "source.txt").write_text("committed source\n")
    (root / "pyproject.toml").write_text('[project]\nversion = "0.1.1rc1"\n')
    (root / "release").mkdir()
    (root / "release/pilot.json").write_text(json.dumps({
        "platform": "linux/amd64",
        "runtime_extras": [],
        "embedding_configuration": None,
        "ask": {
            "mode": "metadata", "keyword_provider": None, "vector_provider": None,
            "answer_provider": None, "models": [],
        },
    }))
    _git(root, "add", ".")
    _git(root, "commit", "--quiet", "-m", "Initial test source")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


@pytest.mark.parametrize("change", ["modified", "staged", "untracked"])
@pytest.mark.parametrize("allow_unmerged", [False, True])
def test_dirty_source_is_rejected_even_for_unmerged_validation(
    assembler, source_checkout, monkeypatch, tmp_path, change, allow_unmerged,
):
    if change == "untracked":
        (source_checkout / "unexpected.txt").write_text("untracked\n")
    else:
        (source_checkout / "source.txt").write_text("modified source\n")
        if change == "staged":
            _git(source_checkout, "add", "source.txt")
    monkeypatch.chdir(source_checkout)
    output = tmp_path / "output"

    with pytest.raises(ValueError, match="source must be clean"):
        assembler.build(output, allow_unmerged, None)

    assert not output.exists()


def test_clean_branch_revision_cannot_be_presented_as_main(
    assembler, source_checkout, monkeypatch, tmp_path,
):
    (source_checkout / "source.txt").write_text("branch revision\n")
    _git(source_checkout, "add", "source.txt")
    _git(source_checkout, "commit", "--quiet", "-m", "Unmerged test change")
    monkeypatch.chdir(source_checkout)
    output = tmp_path / "output"

    with pytest.raises(ValueError, match="source must equal freshly fetched origin/main"):
        assembler.build(output, False, None)

    assert not output.exists()


def test_hosted_success_for_another_source_cannot_qualify_candidate(
    assembler, source_checkout, monkeypatch, tmp_path,
):
    hosted = tmp_path / "hosted.json"
    hosted.write_text(json.dumps({"source_sha": "f" * 40, "status": "passed"}))
    monkeypatch.chdir(source_checkout)
    output = tmp_path / "output"

    with pytest.raises(ValueError, match="Hosted evidence source does not match"):
        assembler.build(output, False, hosted)

    assert not (output / "source.tar").exists()


@pytest.fixture
def hosted_evidence():
    source = "a" * 40
    return {
        "source_sha": source,
        "status": "passed",
        "gates": [
            {
                "workflow": workflow, "required": True, "status": "passed",
                "head_sha": source, "event": "push", "conclusion": "success",
                "run_id": number, "run_attempt": 1,
                "url": f"https://github.com/melliott18/CogniStore/actions/runs/{number}",
            }
            for number, workflow in enumerate(("ci.yml", "kubernetes.yml", "terraform.yml"), 1)
        ],
    }


def test_hosted_evidence_requires_all_successful_runs_at_candidate_source(
    assembler, hosted_evidence,
):
    assert assembler.hosted_passed(hosted_evidence, "a" * 40) is True
    assert assembler.hosted_passed(hosted_evidence, "b" * 40) is False
    assert assembler.hosted_passed(None, "a" * 40) is False
    assert assembler.hosted_passed({"source_sha": "a" * 40, "status": "passed"}, "a" * 40) is False


@pytest.mark.parametrize("change", [
    {"status": "failed"}, {"conclusion": "failure"}, {"head_sha": "b" * 40},
    {"required": False}, {"event": "pull_request"}, {"run_id": 0},
    {"run_attempt": 0}, {"url": "https://example.invalid/unrelated-run"},
])
def test_hosted_summary_cannot_hide_a_failed_or_unbound_gate(assembler, hosted_evidence, change):
    evidence = deepcopy(hosted_evidence)
    evidence["gates"][0].update(change)
    assert assembler.hosted_passed(evidence, "a" * 40) is False


def test_duplicate_hosted_workflow_cannot_substitute_for_missing_gate(assembler, hosted_evidence):
    hosted_evidence["gates"][2] = dict(hosted_evidence["gates"][0])
    assert assembler.hosted_passed(hosted_evidence, "a" * 40) is False
