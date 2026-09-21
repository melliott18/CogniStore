"""Offline input checks do not claim deployment or credential verification."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("staging_preflight", ROOT / "scripts/staging_preflight.py")
assert spec and spec.loader
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


@pytest.fixture
def inputs(tmp_path):
    directory = tmp_path / "configuration"
    shutil.copytree(ROOT / "release/staging", directory)
    for name in preflight.FILES:
        path = directory / name
        text = path.read_text()
        import re
        text = re.sub(r"REPLACE_[A-Z0-9_]+", lambda m: (
            "10.42.0.0/24" if m[0].endswith("CIDR") else "fixture-" + m[0][8:].lower()
        ), text)
        path.write_text(text)
    values = yaml.safe_load((directory / "values.yaml").read_text())
    values["auth"] = {"issuer": "https://issuer.invalid", "audience": "staging",
                      "jwksUri": "https://issuer.invalid/jwks"}
    values["image"] = {"repository": "registry.invalid/cognistore", "digest": "sha256:" + "a" * 64}
    role = "arn:aws:iam::123456789012:role/staging-fixture"
    values["serviceAccount"]["annotations"]["eks.amazonaws.com/role-arn"] = role
    next(entry for entry in values["extraEnv"] if entry["name"] == "AWS_ROLE_ARN")["value"] = role
    (directory / "values.yaml").write_text(yaml.safe_dump(values))
    drivers = yaml.safe_load((directory / "drivers.yaml").read_text())
    drivers["tiers"]["warm"]["kms_key_id"] = "arn:aws:kms:us-east-1:123456789012:key/fixture"
    (directory / "drivers.yaml").write_text(yaml.safe_dump(drivers))
    for name in ("authorization.json", "tenants.json"):
        data = json.loads((directory / name).read_text())
        for binding in data["bindings"]:
            binding["issuer"] = "https://issuer.invalid"
        (directory / name).write_text(json.dumps(data))
    env = json.loads((directory / "environment.json").read_text())
    env.update(aws_account_id="123456789012",
               cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/fixture")
    env["spend"] = {"daily_limit_usd": 1, "total_limit_usd": 2,
                    "expires_at": "2099-01-01T00:00:00Z"}
    (directory / "environment.json").write_text(json.dumps(env))
    candidate = tmp_path / "manifest.json"
    candidate.write_text(json.dumps({"source": {"commit": "b" * 40}, "image": {
        "image_id": "sha256:" + "c" * 64, "platform": "linux/amd64",
        "registry_manifest_digest": None}}))
    link = {"schema_version": 1, "candidate_manifest_sha256": preflight.sha256(candidate.read_bytes()),
            "source_commit": "b" * 40, "image_id": "sha256:" + "c" * 64,
            "platform": "linux/amd64", "repository": "registry.invalid/cognistore",
            "registry_manifest_digest": "sha256:" + "a" * 64,
            "transfer_evidence": "restricted://fixture/registry-transfer"}
    (directory / "image-link.json").write_text(json.dumps(link))
    return directory, candidate


def edit(inputs, filename, transform):
    directory, _ = inputs
    path = directory / filename
    data = yaml.safe_load(path.read_text())
    transform(data)
    path.write_text(json.dumps(data) if filename.endswith(".json") else yaml.safe_dump(data))


def test_resolved_input_contract_stays_offline_and_sanitized(inputs):
    report = preflight.inspect(*inputs)
    assert report["status"] == "passed", [k for k, v in report["checks"].items() if not v]
    assert report["production_qualified"] is False
    assert report["deployment_performed"] is False
    output = json.dumps(report)
    assert "issuer.invalid" not in output
    assert "123456789012" not in output
    assert "restricted://" not in output
    assert "fixture-replace" not in output
    assert len(report["configuration_sha256"]) == 64


def test_shipped_templates_cannot_pass(tmp_path):
    report = preflight.inspect(ROOT / "release/staging", tmp_path / "missing.json")
    assert report["status"] == "incomplete"
    assert not report["checks"]["image.candidate_link"]
    assert not report["checks"]["environment.identity_and_bounds"]


@pytest.mark.parametrize("field,value", [
    ("candidate_manifest_sha256", "d" * 64), ("source_commit", "d" * 40),
    ("image_id", "sha256:" + "d" * 64), ("platform", "linux/arm64"),
    ("repository", "another.invalid/image"), ("registry_manifest_digest", "sha256:" + "d" * 64),
    ("transfer_evidence", ""), ("transfer_evidence", "REPLACE_EVIDENCE"),
])
def test_candidate_transfer_mismatch_fails(inputs, field, value):
    edit(inputs, "image-link.json", lambda d: d.update({field: value}))
    assert not preflight.inspect(*inputs)["checks"]["image.candidate_link"]


def test_docker_configuration_digest_is_not_registry_manifest(inputs):
    edit(inputs, "image-link.json", lambda d: d.update(registry_manifest_digest="sha256:" + "c" * 64))
    edit(inputs, "values.yaml", lambda d: d["image"].update(digest="sha256:" + "c" * 64))
    assert not preflight.inspect(*inputs)["checks"]["image.candidate_link"]


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(securityProfile="development"),
    lambda d: d["api"].update(replicas=2),
    lambda d: d["worker"].update(maxInFlight=8),
    lambda d: d["worker"]["autoscaling"].update(enabled=True),
    lambda d: d["scheduler"].update(enabled=True),
    lambda d: d["nodeSelector"].update({"kubernetes.io/arch": "arm64"}),
    lambda d: d["networkPolicy"].update(enabled=False),
    lambda d: d["networkPolicy"]["egress"][0].update(to=[{"ipBlock": {"cidr": "0.0.0.0/0"}}]),
    lambda d: d["networkPolicy"]["egress"][0].update(to=[{}]),
    lambda d: d["auth"].update(issuer="http://issuer.invalid"),
    lambda d: d["auth"].update(issuer="https://different.invalid"),
])
def test_pilot_drift_fails(inputs, mutation):
    edit(inputs, "values.yaml", mutation)
    assert preflight.inspect(*inputs)["status"] == "incomplete"


@pytest.mark.parametrize("entry", [
    {"name": "COGNISTORE_SECURITY_PROFILE", "value": "development"},
    {"name": "COGNISTORE_TENANT_POLICY", "value": "/tmp/alternate-tenants.json"},
    {"name": "AWS_ACCESS_KEY_ID", "value": "fixture-key"},
    {"name": "AWS_REGION", "value": "us-east-1"},
])
def test_extra_or_duplicate_environment_entries_fail(inputs, entry):
    edit(inputs, "values.yaml", lambda d: d["extraEnv"].append(entry))
    report = preflight.inspect(*inputs)
    assert report["status"] == "incomplete"
    assert not report["checks"]["profile.workload_identity_environment"]


@pytest.mark.parametrize("name,value", [
    ("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/other"),
    ("AWS_WEB_IDENTITY_TOKEN_FILE", "/tmp/other-token"),
    ("AWS_REGION", "us-west-2"), ("AWS_DEFAULT_REGION", "us-west-2"),
    ("AWS_STS_REGIONAL_ENDPOINTS", "legacy"), ("AWS_EC2_METADATA_DISABLED", "false"),
])
def test_workload_identity_environment_must_match_selected_values(inputs, name, value):
    edit(inputs, "values.yaml", lambda d: next(
        entry for entry in d["extraEnv"] if entry["name"] == name
    ).update(value=value))
    assert not preflight.inspect(*inputs)["checks"]["profile.workload_identity_environment"]


def test_duplicate_environment_cannot_hide_a_missing_expected_entry(inputs):
    edit(inputs, "values.yaml", lambda d: d["extraEnv"][-1].update(d["extraEnv"][0]))
    assert not preflight.inspect(*inputs)["checks"]["profile.workload_identity_environment"]


def test_unknown_environment_cannot_replace_expected_entry(inputs):
    edit(inputs, "values.yaml", lambda d: d["extraEnv"][-1].update(
        name="COGNISTORE_SECURITY_PROFILE", value="development"))
    assert not preflight.inspect(*inputs)["checks"]["profile.workload_identity_environment"]


@pytest.mark.parametrize("rules", [
    [{}], [],
    [{"from": [{}], "ports": [{"protocol": "TCP", "port": 8080}]}],
])
def test_unrestricted_or_missing_ingress_fails(inputs, rules):
    edit(inputs, "values.yaml", lambda d: d["networkPolicy"].update(ingress=rules))
    assert not preflight.inspect(*inputs)["checks"]["profile.network_restricted"]


@pytest.mark.parametrize("mutation", [
    lambda r: r.pop("from"),
    lambda r: r.pop("ports"),
    lambda r: r.update(ports=[{}]),
    lambda r: r.update(ports=[{"protocol": "TCP", "port": 8080, "endPort": 65535}]),
    lambda r: r.update(ports=[{"protocol": "UDP", "port": 8080}]),
    lambda r: r.update(ports=[{"protocol": "TCP", "port": 443}]),
    lambda r: r.update(ports=[{"protocol": "TCP", "port": "http"}]),
    lambda r: r["from"].append({}),
    lambda r: r["from"][0].pop("namespaceSelector"),
    lambda r: r["from"][0].pop("podSelector"),
    lambda r: r["from"][0].update(namespaceSelector={}),
    lambda r: r["from"][0].update(podSelector={"matchLabels": {}}),
    lambda r: r["from"][0]["namespaceSelector"]["matchLabels"].update(
        {"kubernetes.io/metadata.name": ""}),
    lambda r: r["from"][0]["podSelector"]["matchLabels"].update(
        {"app.kubernetes.io/name": ""}),
])
def test_proxy_ingress_requires_both_exact_selectors_and_explicit_api_port(inputs, mutation):
    edit(inputs, "values.yaml", lambda d: mutation(d["networkPolicy"]["ingress"][0]))
    assert not preflight.inspect(*inputs)["checks"]["profile.network_restricted"]


def test_smoke_ingress_cannot_become_arbitrary_same_namespace_access(inputs):
    edit(inputs, "values.yaml", lambda d: d["networkPolicy"]["ingress"][2]["from"][0]
         ["podSelector"]["matchLabels"].pop("app.kubernetes.io/component"))
    assert not preflight.inspect(*inputs)["checks"]["profile.network_restricted"]


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(embedding={"provider": "sample"}),
    lambda d: d["tiers"]["warm"].update(endpoint_url="http://localhost:9000"),
    lambda d: d["tiers"]["warm"].update(access_key="fixture-do-not-disclose"),
    lambda d: d["tiers"]["warm"].update(auto_create_bucket=True),
    lambda d: d["tiers"]["hot"].update(path="/tmp/hot"),
])
def test_wrong_storage_or_provider_fails_without_disclosure(inputs, mutation):
    edit(inputs, "drivers.yaml", mutation)
    report = preflight.inspect(*inputs)
    assert report["status"] == "incomplete"
    assert "fixture-do-not-disclose" not in json.dumps(report)


@pytest.mark.parametrize("value", [None, False, 0, -1, float("nan"), float("inf"), "1"])
def test_spend_requires_finite_positive_number(inputs, value):
    edit(inputs, "environment.json", lambda d: d["spend"].update(daily_limit_usd=value))
    assert not preflight.inspect(*inputs)["checks"]["environment.identity_and_bounds"]


def test_expiry_and_evidence_references_required(inputs):
    edit(inputs, "environment.json", lambda d: d["spend"].update(expires_at="2020-01-01T00:00:00Z"))
    edit(inputs, "environment.json", lambda d: d["references"].update(alert_receipt=""))
    report = preflight.inspect(*inputs)
    assert not report["checks"]["environment.identity_and_bounds"]
    assert not report["checks"]["environment.evidence_references_present"]


@pytest.mark.parametrize("filename", preflight.FILES)
def test_missing_file_is_incomplete(inputs, filename):
    (inputs[0] / filename).unlink()
    assert preflight.inspect(*inputs)["status"] == "incomplete"


@pytest.mark.parametrize("filename,content", [
    ("values.yaml", "securityProfile: production\nsecurityProfile: development\n"),
    ("tenants.json", '{"bindings": [], "bindings": []}'),
    ("values.yaml", "null"), ("values.yaml", "a: 1\n---\nb: 2\n"),
])
def test_ambiguous_or_malformed_inputs_fail(inputs, filename, content):
    (inputs[0] / filename).write_text(content)
    assert preflight.inspect(*inputs)["status"] == "incomplete"


@pytest.mark.parametrize("filename", preflight.FILES)
@pytest.mark.parametrize("content", ["null", "{}", "[]", '"not-a-mapping"'])
def test_empty_or_wrong_shape_input_is_incomplete(inputs, filename, content):
    (inputs[0] / filename).write_text(content)
    report = preflight.inspect(*inputs)
    assert report["status"] == "incomplete"
    assert not report["checks"][f"input.{filename}"]


@pytest.mark.parametrize("content", [
    "", "---\n", "apiVersion: v1\nkind: Namespace\n", "spec: {}\n",
    "apiVersion: v1\nkind: Namespace\nmetadata: {name: ''}\n",
    "apiVersion: v1\nkind: Namespace\nmetadata: {name: staging}\n---\nnull\n",
])
def test_resource_documents_require_kubernetes_identity(inputs, content):
    (inputs[0] / "resources.yaml").write_text(content)
    report = preflight.inspect(*inputs)
    assert report["status"] == "incomplete"
    assert not report["checks"]["input.resources.yaml"]


def test_configuration_identity_changes_when_resource_bytes_change(inputs):
    original = preflight.inspect(*inputs)["configuration_sha256"]
    path = inputs[0] / "resources.yaml"
    path.write_text(path.read_text() + "\n# Reviewed new revision\n")
    assert preflight.inspect(*inputs)["configuration_sha256"] != original


def test_cli_does_not_replace_an_input(inputs):
    with pytest.raises(SystemExit):
        preflight.main(["--configuration", str(inputs[0]), "--candidate", str(inputs[1]),
                        "--output", str(inputs[1])])
