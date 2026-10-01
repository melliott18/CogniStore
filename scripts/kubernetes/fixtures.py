#!/usr/bin/env python3
"""Emit disposable kind dependencies; these credentials are ONLY test fixtures."""

from __future__ import annotations

import json


def dependency(name, image, ports, *, args=(), env=None, uid=10001, mount="/data", health_path=None):
    labels = {"app": name}
    return [
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name},
         "spec": {"selector": labels, "ports": [
             {"name": f"port-{port}", "port": port, "targetPort": port} for port in ports
         ]}},
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": name},
         "spec": {"accessModes": ["ReadWriteOnce"],
                  "resources": {"requests": {"storage": "1Gi"}}}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                  "selector": {"matchLabels": labels}, "template": {
                      "metadata": {"labels": labels}, "spec": {
                          "automountServiceAccountToken": False,
                          "securityContext": {"runAsNonRoot": True, "runAsUser": uid,
                                              "runAsGroup": uid, "fsGroup": uid,
                                              "seccompProfile": {"type": "RuntimeDefault"}},
                          "containers": [{"name": name, "image": image, "args": list(args),
                                          "env": [{"name": key, "value": value}
                                                  for key, value in (env or {}).items()],
                                          "securityContext": {"allowPrivilegeEscalation": False,
                                                              "capabilities": {"drop": ["ALL"]}},
                                          "resources": {"requests": {"cpu": "25m", "memory": "64Mi"},
                                                        "limits": {"memory": "512Mi"}},
                                          "readinessProbe": {**({"httpGet": {"port": ports[0], "path": health_path}}
                                                                if health_path else {"tcpSocket": {"port": ports[0]}}),
                                                             "periodSeconds": 2},
                                          "volumeMounts": [{"name": "data", "mountPath": mount}]}],
                          "volumes": [{"name": "data", "persistentVolumeClaim": {
                              "claimName": name}}],
                      }}}},
    ]


def main():
    items = []
    items.extend(dependency(
        "postgres", "pgvector/pgvector:0.8.6-pg16-bookworm@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b",
        [5432], uid=999, mount="/var/lib/postgresql/data",
        env={"POSTGRES_USER": "cognistore", "POSTGRES_PASSWORD": "kind-test-only",
             "POSTGRES_DB": "cognistore", "PGDATA": "/var/lib/postgresql/data/pgdata"},
    ))
    items.extend(dependency(
        "nats", "nats:2.10.26-alpine@sha256:d69eb29526c1d98afdfb2e2434763bef77b5f3c83e2e24769c13a4d104be475e",
        [4222, 8222], args=["-js", "-sd", "/data", "-m", "8222"],
    ))
    items.extend(dependency(
        "minio", "cognistore-minio:qualification",
        [9000], args=["server", "/data"], health_path="/minio/health/ready",
        env={"MINIO_ROOT_USER": "cognistore", "MINIO_ROOT_PASSWORD": "kind-test-only"},
    ))
    drivers = """tiers:
  hot:
    driver: s3
    endpoint_url: http://minio:9000
    region_name: us-east-1
    access_key_env: COGNISTORE_MINIO_ACCESS_KEY
    secret_key_env: COGNISTORE_MINIO_SECRET_KEY
    addressing_style: path
    auto_create_bucket: true
"""
    items.extend([
        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "cognistore-config"},
         "type": "Opaque", "stringData": {"drivers.yaml": drivers, "encryption.json": "{}",
                                           "authorization.json": "{}", "tenants.json": "{}",
                                           "schedules.yaml": json.dumps({"jobs": {
                                               "acceptance-disabled": {
                                                   "type": "catalog.scan", "enabled": False,
                                                   "interval_seconds": 86400,
                                                   "payload": {"tier": "hot", "bucket": "helm-acceptance"},
                                               },
                                           }})}},
        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "cognistore-credentials"},
         "type": "Opaque", "stringData": {
             "COGNISTORE_CATALOG_DB": "postgresql://cognistore:kind-test-only@postgres:5432/cognistore",
             "COGNISTORE_NATS_URL": "nats://nats:4222",
             "COGNISTORE_MINIO_ACCESS_KEY": "cognistore",
             "COGNISTORE_MINIO_SECRET_KEY": "kind-test-only"}},
    ])
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))


if __name__ == "__main__":
    main()
