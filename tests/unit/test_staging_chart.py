"""Render the selected staging profile with isolated, synthetic operator inputs.

These are manifest contracts, not claims about a live cluster or qualification.
"""

from __future__ import annotations

import ipaddress
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
STAGING = ROOT / "release" / "staging"
HELM = shutil.which("helm")
FIXTURE_INPUTS = {
    "REPLACE_IMAGE_REPOSITORY": "registry.example.invalid/cognistore",
    "REPLACE_IMAGE_DIGEST": "a" * 64,
    "REPLACE_OIDC_HOST": "identity.example.invalid",
    "REPLACE_OIDC_AUDIENCE": "cognistore-staging",
    "REPLACE_JWKS_HOST_AND_PATH": "identity.example.invalid/jwks",
    "REPLACE_NODE_NAME": "staging-fixture-node",
    "REPLACE_WORKLOAD_ROLE_ARN": "arn:aws:iam::111122223333:role/staging-fixture",
    "REPLACE_PROXY_NAMESPACE": "staging-proxy",
    "REPLACE_PROXY_APP": "auth-proxy",
    "REPLACE_MONITOR_NAMESPACE": "staging-monitoring",
    "REPLACE_MONITOR_APP": "prometheus",
    "REPLACE_POSTGRES_CIDR": "192.0.2.10/32",
    "REPLACE_S3_ENDPOINT_CIDR": "192.0.2.12/32",
    "REPLACE_STS_ENDPOINT_CIDR": "192.0.2.13/32",
    "REPLACE_IDENTITY_ENDPOINT_CIDR": "192.0.2.14/32",
    "REPLACE_HOT_KMS_KEY_ARN": "arn:aws:kms:us-east-1:111122223333:key/fixture",
}


def _fixture_text(filename: str) -> str:
    text = (STAGING / filename).read_text()
    for key, value in FIXTURE_INPUTS.items():
        text = text.replace(key, value)
    # Ignore instructional comments; every actual input must be resolved.
    assert "REPLACE_" not in yaml.safe_dump(list(yaml.safe_load_all(text)))
    return text


@pytest.fixture
def rendered(tmp_path: Path) -> list[dict[str, Any]]:
    if HELM is None:
        pytest.skip("Helm is required for staging render tests")
    values = tmp_path / "staging-values.yaml"
    values.write_text(_fixture_text("values.yaml"))
    result = subprocess.run(
        [HELM, "template", "cognistore-staging", str(ROOT / "helm" / "cognistore"),
         "--namespace", "cognistore-staging", "--values", str(values)],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _pods(documents: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        document["metadata"]["name"]: (
            document["spec"] if document["kind"] == "Pod"
            else document["spec"]["template"]["spec"]
        )
        for document in documents if document["kind"] in {"Deployment", "Job", "Pod"}
    }


def test_staging_input_is_intentionally_unrenderable_before_image_selection() -> None:
    if HELM is None:
        pytest.skip("Helm is required for staging render tests")
    result = subprocess.run(
        [HELM, "template", "cognistore-staging", str(ROOT / "helm" / "cognistore"),
         "--values", str(STAGING / "values.yaml")],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode != 0
    assert "digest" in result.stderr


def test_staging_hooks_and_workloads_share_node_image_and_hot_volume(
    rendered: list[dict[str, Any]],
) -> None:
    pods = _pods(rendered)
    assert set(pods) == {
        f"cognistore-staging-{component}" for component in ("api", "worker", "migrate", "smoke")
    }
    for name, pod in pods.items():
        assert pod["nodeSelector"] == {
            "kubernetes.io/hostname": "staging-fixture-node",
            "kubernetes.io/os": "linux",
            "kubernetes.io/arch": "amd64",
        }, name
        assert pod["automountServiceAccountToken"] is False
        assert pod["securityContext"]["runAsNonRoot"] is True
        volumes = {volume["name"]: volume for volume in pod["volumes"]}
        assert volumes["hot"]["persistentVolumeClaim"]["claimName"] == "cognistore-staging-hot"
        assert volumes["tmp"]["emptyDir"] == {"medium": "Memory", "sizeLimit": "256Mi"}
        assert volumes["shm"]["emptyDir"] == {"medium": "Memory", "sizeLimit": "256Mi"}
        container = pod["containers"][0]
        assert container["image"] == "registry.example.invalid/cognistore@sha256:" + "a" * 64
        mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
        assert mounts["hot"]["mountPath"] == "/var/lib/cognistore/hot"
        assert not mounts["hot"].get("readOnly", False)
        assert mounts["tmp"]["mountPath"] == "/tmp"
        assert mounts["shm"]["mountPath"] == "/dev/shm"
        assert container["securityContext"]["readOnlyRootFilesystem"] is True


def test_staging_fixed_capacity_and_disabled_scheduling(rendered: list[dict[str, Any]]) -> None:
    assert not any(document["kind"] in {
        "HorizontalPodAutoscaler", "ScaledObject", "PersistentVolumeClaim", "Ingress",
    } for document in rendered)
    deployments = [document for document in rendered if document["kind"] == "Deployment"]
    assert len(deployments) == 2
    for deployment in deployments:
        assert deployment["spec"]["replicas"] == 1
        # Rollout overlap remains on the selected node with the same RWO PVC.
        assert deployment["spec"]["strategy"]["rollingUpdate"] == {
            "maxSurge": 1, "maxUnavailable": 0,
        }
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert container["resources"] == {
            "requests": {"cpu": "2", "memory": "2Gi"},
            "limits": {"cpu": "4", "memory": "4Gi"},
        }
    worker = _pods(rendered)["cognistore-staging-worker"]["containers"][0]
    assert "--disable-scheduled-jobs" in worker["args"]
    assert "--schedule-db" not in worker["args"]
    assert worker["args"][worker["args"].index("--max-in-flight") + 1] == "2"


def test_staging_identity_uses_scoped_projection_and_disables_imds_fallback(
    rendered: list[dict[str, Any]],
) -> None:
    for component in ("api", "worker"):
        pod = _pods(rendered)[f"cognistore-staging-{component}"]
        assert pod["serviceAccountName"] == "cognistore-staging"
        volume = next(volume for volume in pod["volumes"] if volume["name"] == "aws-identity")
        assert volume["projected"]["sources"] == [{"serviceAccountToken": {
            "audience": "sts.amazonaws.com", "expirationSeconds": 3600, "path": "token",
        }}]
        container = pod["containers"][0]
        env = {item["name"]: item.get("value") for item in container["env"]}
        assert env["AWS_ROLE_ARN"] == FIXTURE_INPUTS["REPLACE_WORKLOAD_ROLE_ARN"]
        assert env["AWS_WEB_IDENTITY_TOKEN_FILE"] == "/var/run/cognistore-aws/token"
        assert env["AWS_EC2_METADATA_DISABLED"] == "true"
        assert env["AWS_STS_REGIONAL_ENDPOINTS"] == "regional"
        assert not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"} & env.keys()
        mount = next(mount for mount in container["volumeMounts"]
                     if mount["name"] == "aws-identity")
        assert mount["readOnly"] is True
    # The pre-install account does not exist yet; never trust default via IAM.
    assert "serviceAccountName" not in _pods(rendered)["cognistore-staging-migrate"]


def test_staging_network_has_no_unrestricted_ingress_or_data_egress(
    rendered: list[dict[str, Any]],
) -> None:
    policy = next(document["spec"] for document in rendered
                  if document["kind"] == "NetworkPolicy")
    assert set(policy["policyTypes"]) == {"Ingress", "Egress"}
    for rule in policy["ingress"]:
        assert rule["from"] and rule["ports"]
        for peer in rule["from"]:
            assert peer["podSelector"]["matchLabels"]
    cidrs = set()
    for rule in policy["egress"]:
        assert rule["to"] and rule["ports"]
        for peer in rule["to"]:
            if "ipBlock" in peer:
                cidr = peer["ipBlock"]["cidr"]
                assert ipaddress.ip_network(cidr).prefixlen > 0
                cidrs.add(cidr)
            else:
                assert peer["podSelector"]["matchLabels"]
    assert cidrs == {value for key, value in FIXTURE_INPUTS.items() if key.endswith("_CIDR")}


def test_staging_prerequisites_retain_encrypted_shared_volume_and_bootstrap_boundary() -> None:
    resources = list(yaml.safe_load_all(_fixture_text("resources.yaml")))
    assert not any(resource["kind"] == "Secret" for resource in resources)
    namespace = next(resource for resource in resources if resource["kind"] == "Namespace")
    assert namespace["metadata"]["name"] == "cognistore-staging"
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    storage = next(resource for resource in resources if resource["kind"] == "StorageClass")
    assert storage["provisioner"] == "ebs.csi.aws.com"
    assert storage["parameters"]["encrypted"] == "true"
    assert storage["parameters"]["csi.storage.k8s.io/fstype"] == "ext4"
    assert storage["parameters"]["kmsKeyId"] == FIXTURE_INPUTS["REPLACE_HOT_KMS_KEY_ARN"]
    assert storage["reclaimPolicy"] == "Retain"
    assert storage["volumeBindingMode"] == "WaitForFirstConsumer"
    pvc = next(resource for resource in resources if resource["kind"] == "PersistentVolumeClaim")
    assert pvc["metadata"]["namespace"] == "cognistore-staging"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "200Gi"
    assert pvc["spec"]["storageClassName"] == storage["metadata"]["name"]
    policies = {resource["metadata"]["name"]: resource["spec"] for resource in resources
                if resource["kind"] == "NetworkPolicy"}
    assert policies["default-deny"] == {
        "podSelector": {}, "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": [],
    }
    migration = policies["migration-bootstrap"]
    assert migration["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "migration"
    assert migration["ingress"] == []
    assert len(migration["egress"]) == 2
    assert migration["egress"][1] == {
        "to": [{"ipBlock": {"cidr": FIXTURE_INPUTS["REPLACE_POSTGRES_CIDR"]}}],
        "ports": [{"protocol": "TCP", "port": 5432}],
    }
