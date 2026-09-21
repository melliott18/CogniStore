#!/usr/bin/env python3
"""Opt-in HTTPS smoke against an explicitly provisioned, isolated staging service.

Requires two synthetic tenant principals and pre-provisioned tenant buckets.
No infrastructure is created and no settings are inferred from the environment.
The JSON report is deliberately restricted to public identifiers and check results.
Passing this smoke is not a release or production qualification claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

MAX_RESPONSE_BYTES = 64 * 1024
LIMITATIONS = [
    "Operator-supplied environment, candidate and configuration bindings require separate attestation.",
    "API observations do not attest POSIX/S3 driver types, encryption, backup or infrastructure isolation.",
    "No load, recovery, durability, provider, browser, release or production qualification claim.",
    "Cleanup verifies API/catalog absence; backend orphan checks and retained job/audit records are separate.",
    "Uncertain writes or unsettled jobs require operator reconciliation within the reported run prefix.",
]


class SmokeFailure(RuntimeError):
    """Only fixed check names, never endpoint or response text, may be reported."""


@dataclass
class Checks:
    results: list[dict[str, Any]] = field(default_factory=list)
    current: str = "inputs.valid"
    requests: int = 0

    def require(self, condition: bool, name: str) -> None:
        self.current = name
        self.results.append({"name": name, "passed": bool(condition)})
        if not condition:
            raise SmokeFailure(name)


@dataclass(frozen=True)
class Settings:
    origin: str
    plaintext_origin: str
    trusted_ca: ssl.SSLContext
    unrelated_ca: ssl.SSLContext
    tokens: tuple[str, str]
    bucket: str
    bindings: dict[str, str]
    request_timeout: int
    deadline_seconds: int
    poll_interval: int


def _origin(value: str, scheme: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != scheme or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or any(c.isspace() for c in value)
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise SmokeFailure("inputs.origins")
    # Validate malformed ports before issuing any request.
    _ = parsed.port
    return value.rstrip("/")


def _token(path: Path) -> str:
    with path.open("rb") as source:
        encoded = source.read(32 * 1024 + 1)
    if len(encoded) > 32 * 1024:
        raise SmokeFailure("inputs.tokens")
    token = encoded.decode("ascii").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.~+/-]+=*", token):
        raise SmokeFailure("inputs.tokens")
    return token


def settings(args: argparse.Namespace, checks: Checks) -> Settings:
    checks.require(args.run_staging_smoke, "inputs.explicit_opt_in")
    bindings = {name: getattr(args, name) for name in (
        "environment_id", "candidate_id", "configuration_id",
    )}
    checks.require(all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value)
                       for value in bindings.values()), "inputs.public_identifiers")
    checks.current = "inputs.origins"
    origin = _origin(args.base_url, "https")
    plaintext_origin = _origin(args.plaintext_url, "http")
    checks.require(urlsplit(origin).hostname == urlsplit(plaintext_origin).hostname,
                   "inputs.same_plaintext_host")
    checks.require(bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", args.bucket)),
                   "inputs.synthetic_bucket")
    checks.require(1 <= args.request_timeout <= 30
                   and 30 <= args.deadline_seconds <= 900
                   and 1 <= args.poll_interval <= 10, "inputs.bounded_timeouts")
    checks.current = "inputs.tokens"
    tokens = (_token(args.tenant_a_token_file), _token(args.tenant_b_token_file))
    checks.require(tokens[0] != tokens[1], "inputs.distinct_tokens")
    checks.current = "inputs.trust_material"
    # These explicit contexts cannot inherit SSL_CERT_FILE/SSL_CERT_DIR.
    trusted_ca = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    trusted_ca.load_verify_locations(cafile=str(args.ca_file))
    unrelated_ca = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    unrelated_ca.load_verify_locations(cafile=str(args.unrelated_ca_file))
    checks.require(args.ca_file.read_bytes() != args.unrelated_ca_file.read_bytes(),
                   "inputs.distinct_ca_files")
    checks.require(trusted_ca.check_hostname and unrelated_ca.check_hostname
                   and trusted_ca.verify_mode == ssl.CERT_REQUIRED
                   and unrelated_ca.verify_mode == ssl.CERT_REQUIRED, "inputs.verified_tls")
    return Settings(origin, plaintext_origin, trusted_ca, unrelated_ca, tokens, args.bucket,
                    bindings, args.request_timeout, args.deadline_seconds, args.poll_interval)


def _caused_by(error: BaseException, kind: type[BaseException]) -> bool:
    seen: set[int] = set()
    while id(error) not in seen:
        if isinstance(error, kind):
            return True
        seen.add(id(error))
        cause = error.__cause__ or error.__context__
        if cause is None:
            return False
        error = cause
    return False


class Smoke:
    def __init__(self, config: Settings, checks: Checks, run_id: str) -> None:
        self.config = config
        self.checks = checks
        self.prefix = f"staging-smoke/{run_id}/"
        self.deadline = time.monotonic() + config.deadline_seconds
        self.owned: list[tuple[int, str]] = []
        self.uncertain_writes = 0
        self.unsettled_jobs = 0
        self.jobs: list[dict[str, str]] = []

    def request(self, client: httpx.Client, name: str, method: str, path: str,
                expected: int | tuple[int, ...], *, tenant: int | None = None,
                **kwargs: Any) -> httpx.Response:
        self.checks.current = name
        remaining = self.deadline - time.monotonic()
        self.checks.require(remaining > 0 and self.checks.requests < 512, f"{name}.bounded")
        headers = {"Accept-Encoding": "identity", **kwargs.pop("headers", {})}
        if tenant is not None:
            headers["Authorization"] = f"Bearer {self.config.tokens[tenant]}"
        self.checks.current = name
        self.checks.requests += 1
        with client.stream(method, path, headers=headers,
                           timeout=min(self.config.request_timeout, remaining), **kwargs) as stream:
            self.checks.require(stream.headers.get("Content-Encoding", "identity") == "identity",
                               f"{name}.unencoded_response")
            content = bytearray()
            for chunk in stream.iter_raw():
                if time.monotonic() >= self.deadline:
                    self.checks.require(False, f"{name}.deadline")
                if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                    self.checks.require(False, f"{name}.response_bound")
                content.extend(chunk)
            self.checks.require(time.monotonic() < self.deadline, f"{name}.deadline")
            self.checks.require(True, f"{name}.response_bound")
            response = httpx.Response(stream.status_code, headers=stream.headers,
                                      content=bytes(content), request=stream.request)
        statuses = (expected,) if isinstance(expected, int) else expected
        self.checks.require(response.status_code in statuses, f"{name}.status")
        return response

    def object_path(self, tier: str, key: str) -> str:
        return f"/v1/objects/{tier}/{self.config.bucket}/{key}"

    def catalog_path(self, key: str) -> str:
        return f"/v1/catalog/objects/{self.config.bucket}/{key}"

    def payload(self, tenant: int, tier: str) -> bytes:
        return f"CogniStore synthetic staging fixture tenant-{tenant + 1} {tier}.\n".encode()

    def verify_object(self, client: httpx.Client, tenant: int, tier: str, key: str,
                      payload: bytes, name: str) -> None:
        path = self.object_path(tier, key)
        head = self.request(client, f"{name}.head", "HEAD", path, 200, tenant=tenant)
        self.checks.require(head.content == b""
                            and head.headers.get("Content-Length") == str(len(payload))
                            and bool(head.headers.get("ETag")), f"{name}.head_metadata")
        downloaded = self.request(client, f"{name}.get", "GET", path, 200, tenant=tenant)
        self.checks.require(downloaded.content == payload, f"{name}.exact_bytes")
        self.checks.require(hashlib.sha256(downloaded.content).digest()
                            == hashlib.sha256(payload).digest(), f"{name}.sha256")
        self.checks.require(downloaded.headers.get("ETag") == head.headers.get("ETag"),
                            f"{name}.generation")
        for label, requested, start, end in (
            ("closed", "bytes=1-7", 1, 7),
            ("open", "bytes=8-", 8, len(payload) - 1),
            ("suffix", "bytes=-5", len(payload) - 5, len(payload) - 1),
        ):
            partial = self.request(client, f"{name}.range_{label}", "GET", path, 206,
                                   tenant=tenant, headers={"Range": requested})
            self.checks.require(partial.content == payload[start:end + 1]
                                and partial.headers.get("Content-Range")
                                == f"bytes {start}-{end}/{len(payload)}"
                                and partial.headers.get("Content-Length") == str(end - start + 1),
                                f"{name}.range_{label}_bytes")

    def job(self, client: httpx.Client, tenant: int, name: str, job_type: str,
            path: str, body: dict[str, Any]) -> None:
        # An interrupted POST can have enqueued work even without a response.
        self.unsettled_jobs += 1
        response = self.request(client, f"{name}.submit", "POST", path, 202,
                                tenant=tenant, json=body)
        self.checks.current = f"{name}.contract"
        submission = response.json()
        job_id = str(UUID(submission["job_id"]))
        evidence = {"job_id": job_id, "tenant": f"tenant_{tenant + 1}", "check": name,
                    "job_type": job_type, "status": "unknown"}
        self.jobs.append(evidence)
        status_path = f"/v1/jobs/{job_id}"
        self.checks.require(submission["job_type"] == job_type
                            and submission["status_url"] == status_path
                            and response.headers.get("Location") == status_path,
                            f"{name}.contract")
        # Never follow a server-supplied URL with a bearer token.
        for _ in range(120):
            status = self.request(client, f"{name}.poll", "GET", status_path, 200,
                                  tenant=tenant).json()
            self.checks.require(status.get("job_id") == job_id
                                and status.get("job_type") == job_type
                                and status.get("status") in {
                                    "queued", "running", "retrying", "succeeded", "failed",
                                }, f"{name}.status_contract")
            evidence["status"] = status["status"]
            if status["status"] in ("succeeded", "failed"):
                self.unsettled_jobs -= 1
                self.checks.require(status["status"] == "succeeded", f"{name}.succeeded")
                return
            self.checks.require(time.monotonic() + self.config.poll_interval < self.deadline,
                                f"{name}.poll_deadline")
            time.sleep(self.config.poll_interval)
        self.checks.require(False, f"{name}.poll_limit")

    def exercise(self, client: httpx.Client, untrusted: httpx.Client,
                 plaintext: httpx.Client) -> None:
        self.request(client, "health", "GET", "/healthz", 200)
        self.request(client, "readiness", "GET", "/readyz", 200)
        self.request(client, "metrics.private", "GET", "/metrics", 404)
        catalog = "/v1/catalog/objects"
        self.request(client, "auth.missing", "GET", catalog, 401)
        self.request(client, "auth.invalid", "GET", catalog, 401,
                     headers={"Authorization": "Bearer staging-smoke-invalid-token"})
        self.checks.current = "tls.unrelated_ca_rejected"
        try:
            self.request(untrusted, "tls.unrelated_ca", "GET", "/healthz", 200)
        except httpx.ConnectError as error:
            self.checks.require(_caused_by(error, ssl.SSLCertVerificationError),
                                "tls.unrelated_ca_rejected")
        else:
            self.checks.require(False, "tls.unrelated_ca_rejected")
        self.checks.current = "tls.plaintext_rejected"
        try:
            response = self.request(plaintext, "tls.plaintext", "GET", catalog,
                                    (301, 302, 307, 308, 400, 403, 426))
        except httpx.ConnectError as error:
            self.checks.require(_caused_by(error, ConnectionRefusedError),
                                "tls.plaintext_connection_refused")
        else:
            if response.is_redirect:
                destination = urlsplit(response.headers.get("Location", ""))
                expected = urlsplit(self.config.origin)
                self.checks.require(destination.scheme == "https"
                                    and destination.netloc == expected.netloc
                                    and destination.path == catalog
                                    and not destination.query and not destination.fragment,
                                    "tls.plaintext_redirects_to_trusted_origin")
            self.checks.require(True, "tls.plaintext_rejected")

        # Complete all tenant/read-only checks before the first fixture write.
        for tenant in range(2):
            name = f"tenant_{tenant + 1}"
            response = self.request(client, f"{name}.empty_prefix", "GET", catalog, 200,
                                    tenant=tenant, params={"bucket": self.config.bucket,
                                                           "prefix": self.prefix, "limit": 10})
            self.checks.require(response.json().get("items") == [], f"{name}.prefix_unused")
            for tier in ("hot", "warm"):
                self.request(client, f"{name}.{tier}.absent", "HEAD",
                             self.object_path(tier, f"{self.prefix}{tier}.txt"), 404,
                             tenant=tenant)

        for tier in ("hot", "warm"):
            key = f"{self.prefix}{tier}.txt"
            for tenant in range(2):
                name = f"tenant_{tenant + 1}.{tier}"
                self.uncertain_writes += 1
                self.request(client, f"{name}.put", "PUT", self.object_path(tier, key), 201,
                             tenant=tenant, params={"overwrite": "false"},
                             headers={"Content-Type": "text/plain"},
                             content=self.payload(tenant, tier))
                self.uncertain_writes -= 1
                self.owned.append((tenant, key))
                if tenant == 0:
                    self.request(client, f"{tier}.isolation_absent", "GET",
                                 self.object_path(tier, key), 404, tenant=1)
            for tenant in range(2):
                self.verify_object(client, tenant, tier, key, self.payload(tenant, tier),
                                   f"tenant_{tenant + 1}.{tier}.isolated")

        for tenant in range(2):
            for tier in ("hot", "warm"):
                name = f"tenant_{tenant + 1}.{tier}.scan"
                key = f"{self.prefix}{tier}.txt"
                self.job(client, tenant, name, "catalog.scan", "/v1/actions/catalog-scans", {
                    "tier": tier, "bucket": self.config.bucket, "prefix": self.prefix,
                })
                record = self.request(client, f"{name}.effect", "GET", self.catalog_path(key),
                                      200, tenant=tenant).json()
                self.checks.require(record.get("metadata", {}).get("sha256")
                                    == hashlib.sha256(self.payload(tenant, tier)).hexdigest(),
                                    f"{name}.published_sha256")

        for tenant in range(2):
            name = f"tenant_{tenant + 1}.policy"
            self.job(client, tenant, name, "policy.run", "/v1/actions/policy-runs", {
                "bucket": self.config.bucket, "prefix": self.prefix,
                "config": {"policy": "simple", "threshold": 1,
                           "allowed_tiers": ["hot", "warm"]},
            })
            key = f"{self.prefix}hot.txt"
            self.verify_object(client, tenant, "warm", key, self.payload(tenant, "hot"),
                               f"{name}.moved")
            self.request(client, f"{name}.source_absent", "HEAD",
                         self.object_path("hot", key), 404, tenant=tenant)
            record = self.request(client, f"{name}.catalog", "GET", self.catalog_path(key), 200,
                                  tenant=tenant).json()
            self.checks.require(record.get("tier") == "warm", f"{name}.placement_published")
            if tenant == 0:
                self.verify_object(client, 1, "hot", key, self.payload(1, "hot"),
                                   "tenant_1.policy.other_tenant_unchanged")

    def cleanup(self, client: httpx.Client) -> dict[str, Any]:
        cleanup: dict[str, Any] = {"status": "passed", "objects_created": len(self.owned),
                                  "objects_removed": 0, "uncertain_writes": self.uncertain_writes,
                                  "unsettled_jobs": self.unsettled_jobs}
        if self.unsettled_jobs:
            cleanup["status"] = "deferred_unsettled_jobs"
            return cleanup
        self.deadline = time.monotonic() + 30
        for tenant, key in self.owned:
            name = f"cleanup.tenant_{tenant + 1}.{key.rsplit('/', 1)[-1].split('.')[0]}"
            try:
                record = self.request(client, f"{name}.catalog", "GET", self.catalog_path(key),
                                      (200, 404), tenant=tenant)
                if record.status_code == 200:
                    tier = record.json().get("tier")
                    self.checks.require(tier in ("hot", "warm"), f"{name}.known_tier")
                    self.request(client, f"{name}.delete", "DELETE", self.object_path(tier, key),
                                 204, tenant=tenant)
                self.request(client, f"{name}.catalog_absent", "GET", self.catalog_path(key),
                             404, tenant=tenant)
                for tier in ("hot", "warm"):
                    self.request(client, f"{name}.{tier}_absent", "HEAD",
                                 self.object_path(tier, key), 404, tenant=tenant)
                cleanup["objects_removed"] += 1
            except Exception:
                # Continue attempting independent exact fixture keys; never list/delete a bucket.
                cleanup["status"] = "failed"
        if self.uncertain_writes:
            cleanup["status"] = "uncertain_writes_require_reconciliation"
        return cleanup


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--run-staging-smoke", action="store_true",
                        help="explicitly allow synthetic writes to the supplied staging service")
    for flag in ("base-url", "plaintext-url", "bucket", "environment-id", "candidate-id",
                 "configuration-id"):
        result.add_argument(f"--{flag}", required=True)
    for flag in ("ca-file", "unrelated-ca-file", "tenant-a-token-file", "tenant-b-token-file",
                 "output"):
        result.add_argument(f"--{flag}", required=True, type=Path)
    result.add_argument("--request-timeout", type=int, default=10)
    result.add_argument("--deadline-seconds", type=int, default=300)
    result.add_argument("--poll-interval", type=int, default=1)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    checks = Checks()
    run_id = uuid4().hex
    report: dict[str, Any] = {
        "schema_version": 1, "status": "failed", "production_qualified": False,
        "release_qualified": False, "run_id": run_id, "synthetic_prefix": f"staging-smoke/{run_id}/",
        "started_at": datetime.now(timezone.utc).isoformat(), "limitations": LIMITATIONS,
        "hosted_ci": "skipped: user instruction; known GitHub billing/spending restriction",
        "cleanup": {"status": "not_needed", "objects_created": 0, "objects_removed": 0},
    }
    started = time.monotonic()
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        # Reserve a new output before mutation. Never truncate an input/previous evidence file.
        with args.output.open("x", encoding="utf-8") as output:
            try:
                config = settings(args, checks)
                report["bindings"] = config.bindings
                smoke = Smoke(config, checks, run_id)
                report["jobs"] = smoke.jobs
                with (httpx.Client(base_url=config.origin, verify=config.trusted_ca,
                                   trust_env=False, follow_redirects=False) as client,
                      httpx.Client(base_url=config.origin, verify=config.unrelated_ca,
                                   trust_env=False, follow_redirects=False) as untrusted,
                      httpx.Client(base_url=config.plaintext_origin, trust_env=False,
                                   follow_redirects=False) as plaintext):
                    try:
                        smoke.exercise(client, untrusted, plaintext)
                        report["status"] = "passed"
                    except Exception:
                        report["failure"] = {"check": checks.current}
                    finally:
                        report["cleanup"] = smoke.cleanup(client)
                        if report["cleanup"]["status"] != "passed":
                            report["status"] = "failed"
            except Exception:
                report["failure"] = {"check": checks.current}
            report["checks"] = checks.results
            report["passed_count"] = sum(check["passed"] for check in checks.results)
            report["http_request_count"] = checks.requests
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            output.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    except OSError:
        print("staging smoke: failed to reserve or write a new report file")
        return 2
    finally:
        logging.disable(previous_logging_disable)
    print(f"staging smoke: {report['status']} ({report['passed_count']} checks passed)")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
