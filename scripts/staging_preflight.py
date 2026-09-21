#!/usr/bin/env python3
"""Check sanitized staging inputs offline; never provision or certify a deployment.

This is a bounded input-contract check, not a general Kubernetes security scanner.
Reports contain fixed check names and content hashes, not configuration values.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "values.yaml", "resources.yaml", "drivers.yaml", "authorization.json",
    "tenants.json", "environment.json", "image-link.json", "nats-values.yaml",
    "jetstream-jobs.json", "jetstream-dlq.json",
)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pairs_unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


class UniqueLoader(yaml.SafeLoader):
    pass


def yaml_mapping(loader, node):
    return pairs_unique((loader.construct_object(k), loader.construct_object(v))
                        for k, v in node.value)


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, yaml_mapping)


def complete(value) -> bool:
    if isinstance(value, dict):
        return all(complete(k) and complete(v) for k, v in value.items())
    if isinstance(value, list):
        return all(complete(v) for v in value)
    return not isinstance(value, str) or not re.search(r"REPLACE|TODO|CHANGEME", value, re.I)


def https(value) -> bool:
    parts = urlsplit(value)
    return bool(parts.scheme == "https" and parts.hostname and not parts.username
                and not parts.password and not parts.fragment)


def input_shape(name, value) -> bool:
    if name != "resources.yaml":
        return isinstance(value, dict) and bool(value)
    return isinstance(value, list) and bool(value) and all(
        isinstance(resource, dict)
        and all(isinstance(resource.get(key), str) and bool(resource[key].strip())
                for key in ("apiVersion", "kind"))
        and isinstance(resource.get("metadata"), dict)
        and isinstance(resource["metadata"].get("name"), str)
        and bool(resource["metadata"]["name"].strip())
        for resource in value
    )


def staging_ingress(rules) -> bool:
    """Recognize only the selected private proxy, monitor and smoke rules.

    This checks their explicit selectors/ports, not the trustworthiness of
    external labels or additional policies installed in the cluster.
    """
    if not isinstance(rules, list) or len(rules) != 3:
        return False
    kinds = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"from", "ports"}:
            return False
        peers, ports = rule["from"], rule["ports"]
        if (not isinstance(peers, list) or len(peers) != 1
                or not isinstance(ports, list) or not ports):
            return False
        if not all(isinstance(port, dict) and set(port) == {"protocol", "port"}
                   and port["protocol"] == "TCP" and type(port["port"]) is int
                   for port in ports):
            return False
        numbers = {port["port"] for port in ports}
        peer = peers[0]
        if not isinstance(peer, dict):
            return False
        if peer == {"podSelector": {"matchLabels": {
            "app.kubernetes.io/instance": "cognistore-staging",
            "app.kubernetes.io/component": "smoke",
        }}}:
            if numbers != {8080}:
                return False
            kinds.add("smoke")
            continue
        if set(peer) != {"namespaceSelector", "podSelector"}:
            return False
        for selector, label in (("namespaceSelector", "kubernetes.io/metadata.name"),
                                ("podSelector", "app.kubernetes.io/name")):
            selected = peer[selector]
            if not isinstance(selected, dict) or set(selected) != {"matchLabels"}:
                return False
            labels = selected["matchLabels"]
            if (not isinstance(labels, dict) or set(labels) != {label}
                    or not isinstance(labels[label], str) or not labels[label].strip()):
                return False
        if numbers == {8080}:
            kinds.add("proxy")
        elif numbers == {8080, 8081}:
            kinds.add("monitor")
        else:
            return False
    return kinds == {"proxy", "monitor", "smoke"}


def workload_identity(values) -> bool:
    role = values["serviceAccount"]["annotations"]["eks.amazonaws.com/role-arn"]
    expected = {
        "AWS_ROLE_ARN": role,
        "AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/cognistore-aws/token",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_STS_REGIONAL_ENDPOINTS": "regional",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    entries = values["extraEnv"]
    if not isinstance(entries, list) or len(entries) != len(expected):
        return False
    environment = {}
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"name", "value"}
                or not isinstance(entry["name"], str) or entry["name"] in environment):
            return False
        environment[entry["name"]] = entry["value"]
    return (isinstance(role, str)
            and bool(re.fullmatch(r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+", role))
            and environment == expected)


def profile_checks(values, drivers, authorization, tenants) -> dict[str, bool]:
    from cognistore.auth.authorization import RBACPolicy
    from cognistore.auth.tenancy import TenantResolver

    RBACPolicy.from_dict(authorization)
    TenantResolver.from_dict(tenants)
    roles = {(b["issuer"], b["subject"]): set(b["roles"]) for b in authorization["bindings"]}
    memberships = {(b["issuer"], b["subject"]): b["tenant_id"] for b in tenants["bindings"]}
    issuer = values["auth"]["issuer"]
    required_roles = {"reader", "writer", "operator", "auditor", "admin"}
    role_coverage = all(required_roles <= set().union(*(
        roles.get(identity, set()) for identity, owner in memberships.items() if owner == tenant
    )) for tenant in ("pilot-a", "pilot-b"))
    fixed = all(values[k]["replicas"] == 1 and values[k]["autoscaling"]["enabled"] is False
                for k in ("api", "worker"))
    resources = {"requests": {"cpu": "2", "memory": "2Gi"},
                 "limits": {"cpu": "4", "memory": "4Gi"}}
    volumes = {v["name"]: v for v in values["extraVolumes"]}
    mounts = {m["name"]: m for m in values["extraVolumeMounts"]}
    egress = values["networkPolicy"]["egress"]
    narrow = bool(egress)
    for rule in egress:
        narrow &= bool(rule.get("to") and rule.get("ports"))
        for peer in rule.get("to", []):
            if "ipBlock" in peer:
                network = ipaddress.ip_network(peer["ipBlock"]["cidr"], strict=True)
                narrow &= network.prefixlen > 0
            else:
                narrow &= bool(peer.get("podSelector", {}).get("matchLabels"))
    hot, warm = drivers["tiers"]["hot"], drivers["tiers"]["warm"]
    allowed_warm = {"driver", "region_name", "server_side_encryption", "kms_key_id",
                    "auto_create_bucket", "multipart_threshold", "chunk_size"}
    # Kubernetes mount comparison only; no filesystem I/O occurs here.
    shm_mount_path = "/dev/shm"  # nosec B108
    return {
        "profile.production": values["securityProfile"] == "production",
        "profile.workload_identity_environment": workload_identity(values),
        "profile.fixed_capacity": fixed and values["worker"]["maxInFlight"] == 2
            and all(values[k]["resources"] == resources for k in ("api", "worker")),
        "profile.no_scheduler": values["scheduler"]["enabled"] is False,
        "profile.private_proxy_external": values["ingress"]["enabled"] is False,
        "profile.node_placement": bool(values["nodeSelector"]["kubernetes.io/hostname"])
            and values["nodeSelector"]["kubernetes.io/os"] == "linux"
            and values["nodeSelector"]["kubernetes.io/arch"] == "amd64",
        "profile.shared_hot_mount": volumes["hot"]["persistentVolumeClaim"]["claimName"]
            == "cognistore-staging-hot" and mounts["hot"]["mountPath"] == "/var/lib/cognistore/hot",
        "profile.shared_memory_bound": volumes["shm"]["emptyDir"] ==
            {"medium": "Memory", "sizeLimit": "256Mi"}
            and mounts["shm"]["mountPath"] == shm_mount_path,
        "profile.network_restricted": values["networkPolicy"]["enabled"] is True and narrow
            and staging_ingress(values["networkPolicy"]["ingress"]),
        "profile.oidc_https": https(issuer) and https(values["auth"]["jwksUri"])
            and bool(values["auth"]["audience"]),
        "profile.two_tenants": set(memberships.values()) == {"pilot-a", "pilot-b"}
            and set(roles) == set(memberships) and all(k[0] == issuer for k in roles)
            and role_coverage,
        "profile.metadata_only_storage": set(drivers) == {"tiers"}
            and set(drivers["tiers"]) == {"hot", "warm"}
            and hot == {"driver": "posix", "path": "/var/lib/cognistore/hot"}
            and set(warm) == allowed_warm and warm["driver"] == "s3"
            and warm["region_name"] == "us-east-1" and warm["auto_create_bucket"] is False
            and warm["server_side_encryption"] == "aws:kms"
            and bool(re.fullmatch(r"arn:aws:kms:us-east-1:[0-9]{12}:key/[A-Za-z0-9-]+", warm["kms_key_id"]))
            and warm["multipart_threshold"] == warm["chunk_size"] == 8388608,
    }


def inspect(configuration: Path, candidate: Path) -> dict:
    checks: dict[str, bool] = {}
    hashes: dict[str, str] = {}
    documents = {}
    for name in FILES:
        try:
            raw = (configuration / name).read_bytes()
            hashes[name] = sha256(raw)
            if name.endswith(".json"):
                value = json.loads(raw, object_pairs_hook=pairs_unique)
            else:
                items = list(yaml.load_all(raw, Loader=UniqueLoader))
                value = items if name == "resources.yaml" else items[0]
                if name != "resources.yaml" and len(items) != 1:
                    raise ValueError("multiple documents")
            documents[name] = value
            checks[f"input.{name}"] = input_shape(name, value) and complete(value)
        except (OSError, ValueError, TypeError, IndexError, yaml.YAMLError):
            checks[f"input.{name}"] = False
    try:
        raw = candidate.read_bytes()
        manifest = json.loads(raw, object_pairs_hook=pairs_unique)
        hashes["candidate-manifest"] = sha256(raw)
        link = documents["image-link.json"]
        image = documents["values.yaml"]["image"]
        checks["image.candidate_link"] = (
            link["schema_version"] == 1
            and link["candidate_manifest_sha256"] == hashes["candidate-manifest"]
            and link["source_commit"] == manifest["source"]["commit"]
            and link["image_id"] == manifest["image"]["image_id"]
            and link["platform"] == manifest["image"]["platform"] == "linux/amd64"
            and link["repository"] == image["repository"]
            and link["registry_manifest_digest"] == image["digest"]
            and bool(DIGEST.fullmatch(image["digest"]))
            and bool(DIGEST.fullmatch(link["image_id"]))
            # Docker configuration and registry manifest digests are distinct.
            and image["digest"] != link["image_id"]
            and manifest["image"].get("registry_manifest_digest") in (None, image["digest"])
            and bool(link["transfer_evidence"]) and complete(link)
        )
    except (OSError, ValueError, TypeError, KeyError):
        checks["image.candidate_link"] = False
    try:
        checks.update(profile_checks(documents["values.yaml"], documents["drivers.yaml"],
                                     documents["authorization.json"], documents["tenants.json"]))
    except (ValueError, TypeError, KeyError, AttributeError):
        checks["profile.valid_structure"] = False
    try:
        env = documents["environment.json"]
        spend = env["spend"]
        expires = datetime.fromisoformat(spend["expires_at"].replace("Z", "+00:00"))
        positive = all(type(spend[k]) in (int, float) and math.isfinite(spend[k]) and spend[k] > 0
                       for k in ("daily_limit_usd", "total_limit_usd"))
        checks["environment.identity_and_bounds"] = (
            env["schema_version"] == 1 and complete(env)
            and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", env["environment_id"]))
            and bool(re.fullmatch(r"[0-9]{12}", env["aws_account_id"]))
            and env["region"] == "us-east-1" and env["namespace"] == "cognistore-staging"
            and env["cluster_arn"].startswith(f"arn:aws:eks:us-east-1:{env['aws_account_id']}:cluster/")
            and env["data_classification"] == "synthetic-only"
            and all(isinstance(env["owners"][k], str) and env["owners"][k].strip()
                    for k in ("operations", "security", "recovery", "cost"))
            and positive and spend["total_limit_usd"] >= spend["daily_limit_usd"]
            and expires > datetime.now(timezone.utc)
        )
        checks["environment.evidence_references_present"] = all(
            isinstance(env["references"][k], str) and bool(env["references"][k].strip())
            and complete(env["references"][k]) for k in (
                "specification_review", "access_review", "secret_versions", "platform_encryption",
                "backup_recovery", "network_policy", "oidc_proxy", "telemetry", "alert_receipt")
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        checks["environment.identity_and_bounds"] = False
    configuration_hashes = {k: v for k, v in hashes.items() if k != "candidate-manifest"}
    return {
        "schema_version": 1, "scope": "offline-staging-input-contract",
        "status": "passed" if checks and all(checks.values()) else "incomplete",
        "production_qualified": False, "deployment_performed": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "configuration_sha256": sha256(json.dumps(configuration_hashes, sort_keys=True).encode()),
        "sha256": hashes, "checks": checks,
        "limitations": [
            "Checks input consistency only; evidence references and registry provenance require review.",
            "Does not validate arbitrary Helm overrides, rendered resources or live infrastructure.",
            "No platform encryption, backup, network enforcement, OIDC, notification or spend verification.",
            "Candidate release blockers, hosted gates and owner/pilot decisions remain independent.",
        ],
        "hosted_ci": "skipped: user instruction; known GitHub billing/spending restriction",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", type=Path, default=ROOT / "release/staging")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.resolve() in {args.candidate.resolve(), *(
        (args.configuration / name).resolve() for name in FILES
    )}:
        parser.error("output must not replace an input")
    report = inspect(args.configuration, args.candidate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"staging input contract: {report['status']}")
    for name, passed in report["checks"].items():
        if not passed:
            print(f"  incomplete: {name}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
