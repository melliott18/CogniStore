"""Render real Helm manifests and protect deployment safety contracts.

These checks require Helm; live controller behavior is exercised separately by
scripts/kubernetes/verify.sh.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "helm" / "cognistore"
HELM = shutil.which("helm")
pytestmark = pytest.mark.skipif(HELM is None, reason="Helm is required for chart contract tests")

PRODUCTION = {
    "config": {"existingSecret": "runtime-config"},
    "credentials": {"existingSecret": "runtime-credentials"},
    "tls": {"existingSecret": "runtime-tls"},
    "auth": {"issuer": "https://identity.example.com", "audience": "cognistore"},
}


def _merge(target: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def _render(
    tmp_path: Path, updates: dict[str, Any] | None = None, *, succeeds: bool = True,
) -> list[dict[str, Any]] | str:
    values = copy.deepcopy(PRODUCTION)
    _merge(values, updates or {})
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values))
    assert HELM is not None
    result = subprocess.run(
        [HELM, "template", "contract", str(CHART), "--namespace", "cognistore-test",
         "--values", str(path)],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if not succeeds:
        assert result.returncode != 0, "unsafe values unexpectedly rendered"
        return result.stderr
    assert result.returncode == 0, result.stderr
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def _resource(documents: list[dict[str, Any]], kind: str, suffix: str = "") -> dict[str, Any]:
    matches = [item for item in documents if item["kind"] == kind
               and item["metadata"]["name"].endswith(suffix)]
    assert len(matches) == 1, (kind, suffix, matches)
    return matches[0]


def _pod(resource: dict[str, Any]) -> dict[str, Any]:
    return resource["spec"] if resource["kind"] == "Pod" else resource["spec"]["template"]["spec"]


def _env(container: dict[str, Any]) -> dict[str, Any]:
    return {item["name"]: item.get("value", item.get("valueFrom")) for item in container["env"]}


def test_production_workloads_are_unprivileged_and_reference_secrets(tmp_path: Path) -> None:
    documents = _render(tmp_path)
    assert isinstance(documents, list)
    assert not any(item["kind"] in {"Secret", "Role", "RoleBinding", "ClusterRoleBinding"}
                   for item in documents)
    for item in documents:
        if item["kind"] not in {"Deployment", "Job", "Pod"}:
            continue
        pod = _pod(item)
        assert pod["automountServiceAccountToken"] is False
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["securityContext"]["runAsUser"] > 0
        assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        volumes = {volume["name"]: volume for volume in pod["volumes"]}
        assert volumes["config"]["secret"]["secretName"] == "runtime-config"
        assert volumes["tls"]["secret"]["secretName"] == "runtime-tls"
        assert volumes["tmp"]["emptyDir"]["medium"] == "Memory"
        for container in pod["containers"]:
            security = container["securityContext"]
            assert security["allowPrivilegeEscalation"] is False
            assert security["readOnlyRootFilesystem"] is True
            assert security["capabilities"]["drop"] == ["ALL"]
            assert container["resources"]["requests"]["cpu"]
            assert container["resources"]["requests"]["memory"]
            for mount in container["volumeMounts"]:
                if mount["name"] in {"config", "tls"}:
                    assert mount["readOnly"] is True
            if item["kind"] != "Pod":
                assert container["envFrom"] == [{"secretRef": {"name": "runtime-credentials"}}]
                env = _env(container)
                assert env["COGNISTORE_SECURITY_PROFILE"] == "production"
                assert env["COGNISTORE_AUTH_ISSUER"] == PRODUCTION["auth"]["issuer"]
                assert env["COGNISTORE_AUTHORIZATION_POLICY"].endswith("/authorization.json")
                assert env["COGNISTORE_TENANT_POLICY"].endswith("/tenants.json")


def test_stateless_workers_have_no_local_schedule_store(tmp_path: Path) -> None:
    documents = _render(tmp_path)
    assert isinstance(documents, list)
    assert not any(item["kind"] == "PersistentVolumeClaim" for item in documents)
    assert not any(item["metadata"]["name"].endswith("-scheduler") for item in documents)
    worker = _pod(_resource(documents, "Deployment", "-worker"))
    args = worker["containers"][0]["args"]
    assert "--disable-scheduled-jobs" in args
    assert "--schedule-db" not in args
    assert not any(volume.get("persistentVolumeClaim") for volume in worker["volumes"])


def test_node_placement_applies_to_workloads_and_install_test_hooks(tmp_path: Path) -> None:
    selector = {"cognistore.example.com/pool": "storage"}
    tolerations = [{"key": "storage", "operator": "Equal", "value": "cognistore",
                    "effect": "NoSchedule"}]
    documents = _render(tmp_path, {
        "nodeSelector": selector,
        "tolerations": tolerations,
        "scheduler": {"enabled": True, "principalIssuer": "https://identity.example.com",
                      "principalSubject": "scheduler"},
    })
    assert isinstance(documents, list)
    pods = [item for item in documents if item["kind"] in {"Deployment", "Job", "Pod"}]
    assert len(pods) == 5
    for item in pods:
        assert _pod(item)["nodeSelector"] == selector, item["metadata"]["name"]
        assert _pod(item)["tolerations"] == tolerations, item["metadata"]["name"]
    smoke = _resource(documents, "Pod", "-smoke")
    # `helm test --logs` reads the successful Pod after completion. Deleting it
    # on success makes a passing clean install report a test failure.
    policy = smoke["metadata"]["annotations"]["helm.sh/hook-delete-policy"].split(",")
    assert policy == ["before-hook-creation"]


def test_default_network_boundary_allows_only_dns(tmp_path: Path) -> None:
    documents = _render(tmp_path)
    assert isinstance(documents, list)
    policy = _resource(documents, "NetworkPolicy")["spec"]
    assert set(policy["policyTypes"]) == {"Ingress", "Egress"}
    assert policy["ingress"] == []
    assert len(policy["egress"]) == 1
    dns = policy["egress"][0]
    assert {port["port"] for port in dns["ports"]} == {53}
    assert {port["protocol"] for port in dns["ports"]} == {"TCP", "UDP"}
    assert dns["to"][0]["namespaceSelector"]["matchLabels"]
    assert dns["to"][0]["podSelector"]["matchLabels"]


def test_production_probes_distinguish_liveness_and_readiness(tmp_path: Path) -> None:
    documents = _render(tmp_path)
    assert isinstance(documents, list)
    for component in ("api", "worker"):
        container = _pod(_resource(documents, "Deployment", f"-{component}"))["containers"][0]
        for probe, path in (("startupProbe", "/healthz"), ("livenessProbe", "/healthz"),
                            ("readinessProbe", "/readyz")):
            assert container[probe]["httpGet"]["scheme"] == "HTTPS"
            assert container[probe]["httpGet"]["path"] == path
        env = _env(container)
        assert env["COGNISTORE_API_TLS_CERTFILE"].endswith("/tls.crt")
        assert env["COGNISTORE_HEALTH_TLS_CERTFILE"].endswith("/tls.crt")
    worker = _pod(_resource(documents, "Deployment", "-worker"))
    args = worker["containers"][0]["args"]
    drain = int(args[args.index("--shutdown-grace") + 1])
    settle = int(args[args.index("--settlement-timeout") + 1])
    assert worker["terminationGracePeriodSeconds"] > drain + settle + 5


@pytest.mark.parametrize("existing_claim", ["", "retained-scheduler-state"])
def test_scheduled_workers_share_one_node_and_retained_volume(
    tmp_path: Path, existing_claim: str,
) -> None:
    documents = _render(tmp_path, {"scheduler": {
        "enabled": True, "principalIssuer": "https://identity.example.com",
        "principalSubject": "scheduler", "storage": {"existingClaim": existing_claim},
    }})
    assert isinstance(documents, list)
    scheduler = _resource(documents, "Deployment", "-scheduler")
    worker = _resource(documents, "Deployment", "-worker")
    assert scheduler["spec"]["replicas"] == 1
    assert scheduler["spec"]["strategy"]["type"] == "Recreate"
    claims = []
    for deployment in (scheduler, worker):
        pod = _pod(deployment)
        terms = pod["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        assert len(terms) == 1
        assert terms[0]["topologyKey"] == "kubernetes.io/hostname"
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        assert all(labels[key] == value
                   for key, value in terms[0]["labelSelector"]["matchLabels"].items())
        volumes = {volume["name"]: volume for volume in pod["volumes"]}
        claims.append(volumes["schedule"]["persistentVolumeClaim"]["claimName"])
        args = pod["containers"][0]["args"]
        assert "--disable-scheduled-jobs" not in args
        assert args[args.index("--schedule-db") + 1].endswith("/schedule.sqlite3")
    assert claims[0] == claims[1]
    if existing_claim:
        assert claims[0] == existing_claim
        assert not any(item["kind"] == "PersistentVolumeClaim" for item in documents)
    else:
        pvc = _resource(documents, "PersistentVolumeClaim")
        assert pvc["metadata"]["name"] == claims[0]
        assert pvc["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
        assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    container = _pod(scheduler)["containers"][0]
    assert "--principal-issuer" in container["args"]
    for probe in ("startupProbe", "livenessProbe", "readinessProbe"):
        command = container[probe]["exec"]["command"]
        assert "cognistore.jobs.scheduler_probe" in command
        assert ("--readiness" in command) == (probe == "readinessProbe")
    assert "affinity" not in _pod(_resource(documents, "Deployment", "-api"))


def test_autoscalers_own_replicas_and_share_the_worker_queue_identity(tmp_path: Path) -> None:
    documents = _render(tmp_path, {
        "queue": {"stream": "TEAM_JOBS", "consumer": "team-workers", "subject": "team.jobs"},
        "api": {"autoscaling": {"enabled": True}},
        "worker": {"autoscaling": {"enabled": True,
                                    "natsMonitoringEndpoint": "monitor.example.com:8222"}},
    })
    assert isinstance(documents, list)
    for component in ("api", "worker"):
        assert "replicas" not in _resource(documents, "Deployment", f"-{component}")["spec"]
    hpa = _resource(documents, "HorizontalPodAutoscaler")["spec"]
    assert hpa["scaleTargetRef"]["name"].endswith("-api")
    assert hpa["metrics"][0]["pods"]["metric"]["name"] == "cognistore_http_requests_per_second"
    scaler = _resource(documents, "ScaledObject")["spec"]
    assert scaler["scaleTargetRef"]["name"].endswith("-worker")
    assert scaler["minReplicaCount"] >= 1
    trigger = scaler["triggers"][0]
    assert trigger["type"] == "nats-jetstream"
    assert trigger["metadata"]["useHttps"] == "true"
    worker = _pod(_resource(documents, "Deployment", "-worker"))["containers"][0]
    env = _env(worker)
    for key, option in (("stream", "--job-stream"), ("consumer", "--job-consumer")):
        assert trigger["metadata"][key] == worker["args"][worker["args"].index(option) + 1]
        assert trigger["metadata"][key] == env[f"COGNISTORE_JOB_{key.upper()}"]


def test_migration_hook_upgrades_all_configured_tenants_without_rollback_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = _render(tmp_path)
    assert isinstance(documents, list)
    migration = _resource(documents, "Job", "-migrate")
    hooks = migration["metadata"]["annotations"]["helm.sh/hook"].split(",")
    assert set(hooks) == {"pre-install", "pre-upgrade"}
    assert "serviceAccountName" not in _pod(migration)
    policy = tmp_path / "tenants.json"
    policy.write_text(json.dumps({"bindings": [
        {"issuer": "https://id.example.com", "subject": "a", "tenant_id": "alpha"},
        {"issuer": "https://id.example.com", "subject": "b", "tenant_id": "beta"},
        {"issuer": "https://id.example.com", "subject": "a2", "tenant_id": "alpha"},
    ]}))
    migrated = []

    class Catalog:
        def for_tenant(self, tenant_id: str) -> None:
            migrated.append(tenant_id)

        def close(self) -> None:
            migrated.append("closed")

    def open_catalog(locator: str) -> Catalog:
        assert locator == "postgresql://catalog.example.com/cognistore"
        migrated.append("default")
        return Catalog()

    # Keep render validation independent of the database runtime installation;
    # the real tenant-policy parser still validates the migration input.
    database_module = types.ModuleType("cognistore.db")
    database_module.open_catalog = open_catalog
    monkeypatch.setitem(sys.modules, "cognistore.db", database_module)
    monkeypatch.setenv("COGNISTORE_CATALOG_DB", "postgresql://catalog.example.com/cognistore")
    monkeypatch.setenv("COGNISTORE_TENANT_POLICY", str(policy))
    exec(_pod(migration)["containers"][0]["args"][0], {})
    assert migrated == ["default", "alpha", "beta", "closed"]


@pytest.mark.parametrize(("updates", "error"), [
    ({"config": {"existingSecret": ""}}, "config.existingSecret is required"),
    ({"credentials": {"existingSecret": ""}}, "credentials.existingSecret is required"),
    ({"tls": {"existingSecret": ""}}, "production requires"),
    ({"auth": {"issuer": ""}}, "production requires"),
    ({"auth": {"audience": ""}}, "production requires"),
    ({"scheduler": {"enabled": True}}, "production scheduler requires"),
    ({"worker": {"autoscaling": {"enabled": True}}}, "natsMonitoringEndpoint is required"),
    ({"api": {"autoscaling": {"enabled": True, "minReplicas": 9}}},
     "minReplicas must not exceed maxReplicas"),
    ({"worker": {"autoscaling": {"enabled": True, "minReplicas": 9,
                                   "natsMonitoringEndpoint": "monitor:8222"}}},
     "minReplicas must not exceed maxReplicas"),
    ({"worker": {"terminationGracePeriodSeconds": 40}}, "termination grace must exceed"),
    ({"worker": {"autoscaling": {"minReplicas": 0}}}, "minReplicas"),
    ({"api": {"autoscaling": {"metrics": []}}}, "metrics"),
    ({"securityProfile": "unsafe"}, "securityProfile"),
    ({"image": {"digest": "sha256:incorrect"}}, "digest"),
])
def test_unsafe_configuration_is_rejected_before_install(
    tmp_path: Path, updates: dict[str, Any], error: str,
) -> None:
    message = _render(tmp_path, updates, succeeds=False)
    assert isinstance(message, str)
    assert error in message
