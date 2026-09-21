"""Local mock HTTP tests; these are not real staging acceptance evidence."""

from __future__ import annotations

import hashlib
import json
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from scripts import staging_smoke

TOKENS = ("synthetic-tenant-a-private-token", "synthetic-tenant-b-private-token")
PRIVATE_ENDPOINT = "https://private-staging.invalid:8443"
PRIVATE_EXCEPTION = "private credential and service response must not appear"
HTTPX_CLIENT = httpx.Client


@pytest.fixture(scope="module")
def ca_files(tmp_path_factory):
    root = tmp_path_factory.mktemp("smoke-ca")
    files = []
    for index in range(2):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"synthetic-ca-{index}")])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                       .public_key(key.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(now - timedelta(days=1))
                       .not_valid_after(now + timedelta(days=1))
                       .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                       .sign(key, hashes.SHA256()))
        path = root / f"ca-{index}.pem"
        path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        files.append(path)
    return files


@pytest.fixture
def arguments(tmp_path, ca_files):
    tokens = []
    for index, token in enumerate(TOKENS):
        path = tmp_path / f"tenant-{index}.token"
        path.write_text(token)
        tokens.append(path)
    return [
        "--run-staging-smoke", "--base-url", PRIVATE_ENDPOINT,
        "--plaintext-url", "http://private-staging.invalid:8080", "--bucket", "smoke-fixtures",
        "--environment-id", "staging-01", "--candidate-id", "candidate-01",
        "--configuration-id", "config-01", "--ca-file", str(ca_files[0]),
        "--unrelated-ca-file", str(ca_files[1]),
        "--tenant-a-token-file", str(tokens[0]), "--tenant-b-token-file", str(tokens[1]),
        "--output", str(tmp_path / "report.json"),
    ]


def respond(status, body=None, *, content=b"", headers=None):
    if body is not None:
        content = json.dumps(body).encode()
    # Streaming is deliberate: the smoke bounds bytes before reading JSON.
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(content))


class FixtureService:
    """Tiny deterministic transport fixture, never a substitute for real services."""

    def __init__(self, mode="success"):
        self.mode = mode
        self.requests = []
        self.records = {}
        self.objects = {}
        self.jobs = {}
        self.deleted = []
        self.client_options = []

    def client(self, **kwargs):
        role = ("trusted", "unrelated", "plaintext")[len(self.client_options)]
        self.client_options.append(kwargs)
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return HTTPX_CLIENT(**kwargs, transport=httpx.MockTransport(
            lambda request: self.handle(role, request),
        ))

    def handle(self, role, request):
        self.requests.append((role, request))
        path = request.url.path
        if role == "unrelated":
            assert "Authorization" not in request.headers
            if self.mode == "unrelated_ca_accepted":
                return respond(200)
            cause = (RuntimeError(PRIVATE_EXCEPTION) if self.mode == "unrelated_network_failure"
                     else ssl.SSLCertVerificationError(1, PRIVATE_EXCEPTION))
            raise httpx.ConnectError(PRIVATE_EXCEPTION) from cause
        if role == "plaintext":
            assert "Authorization" not in request.headers
            if self.mode == "plaintext_accepted":
                return respond(401)
            return respond(308, headers={"Location": f"{PRIVATE_ENDPOINT}{path}"})
        if path in ("/healthz", "/readyz"):
            return respond(200)
        if path == "/metrics":
            return respond(200 if self.mode == "public_metrics" else 404)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in TOKENS:
            return respond(200 if self.mode == "anonymous_access" else 401)
        tenant = TOKENS.index(token)
        if self.mode == "bearer_redirect":
            return respond(307, headers={"Location": "https://credential-thief.invalid/"})
        if path == "/v1/catalog/objects":
            items = [record for (owner, key), record in self.records.items()
                     if owner == tenant and key.startswith(request.url.params["prefix"])]
            if self.mode == "occupied_prefix":
                items = [{"key": "already-present"}]
            return respond(200, {"items": items})
        if path.startswith("/v1/catalog/objects/"):
            key = path.split("/", 5)[5]
            record = self.records.get((tenant, key))
            return respond(200, record) if record else respond(404)
        if path.startswith("/v1/actions/"):
            body = json.loads(request.content)
            assert body["prefix"].startswith("staging-smoke/")
            assert body["prefix"].endswith("/")
            assert body["bucket"] == "smoke-fixtures"
            job_id = str(uuid4())
            job_type = "catalog.scan" if path.endswith("catalog-scans") else "policy.run"
            status_path = f"/v1/jobs/{job_id}"
            job = {"job_id": job_id, "job_type": job_type, "status": "queued",
                   "status_url": status_path}
            self.jobs[job_id] = (tenant, body, job)
            if self.mode == "job_url_redirect":
                job["status_url"] = "https://credential-thief.invalid/v1/jobs/anything"
            return respond(202, job, headers={"Location": job["status_url"]})
        if path.startswith("/v1/jobs/"):
            owner, body, job = self.jobs[path.rsplit("/", 1)[1]]
            assert owner == tenant
            if self.mode == "job_failed":
                job["status"] = "failed"
            elif self.mode != "job_never_terminal":
                job["status"] = "succeeded"
                for (owner, key), record in list(self.records.items()):
                    if owner != tenant or not key.startswith(body["prefix"]):
                        continue
                    tier = record["tier"]
                    payload = self.objects[tenant, tier, key]
                    if job["job_type"] == "catalog.scan":
                        if tier == body["tier"] and self.mode != "scan_without_effect":
                            record["metadata"]["sha256"] = hashlib.sha256(payload).hexdigest()
                    elif tier == "hot" and self.mode != "policy_without_effect":
                        self.objects[tenant, "warm", key] = self.objects.pop((tenant, "hot", key))
                        record["tier"] = "warm"
            return respond(200, job)
        assert path.startswith("/v1/objects/")
        _, _, _, tier, bucket, key = path.split("/", 5)
        assert bucket == "smoke-fixtures"
        if self.mode == "tenant_alias":
            tenant = 0
        identity = (tenant, tier, key)
        if request.method == "PUT":
            assert request.url.params["overwrite"] == "false"
            assert identity not in self.objects
            self.objects[identity] = request.content
            self.records[tenant, key] = {"tier": tier, "key": key, "metadata": {}}
            if self.mode == "uncertain_write":
                raise httpx.ReadTimeout(PRIVATE_EXCEPTION)
            return respond(201)
        if identity not in self.objects:
            return respond(404)
        payload = self.objects[identity]
        if request.method == "DELETE":
            if self.mode == "cleanup_failure":
                return respond(503, {"private": PRIVATE_EXCEPTION})
            self.deleted.append(identity)
            del self.objects[identity]
            del self.records[tenant, key]
            return respond(204)
        if request.method == "HEAD":
            return respond(200, headers={"Content-Length": str(len(payload)), "ETag": "synthetic"})
        if "Range" in request.headers:
            requested = request.headers["Range"]
            start, end = {"bytes=1-7": (1, 7), "bytes=8-": (8, len(payload) - 1),
                          "bytes=-5": (len(payload) - 5, len(payload) - 1)}[requested]
            return respond(206, content=payload[start:end + 1], headers={
                "Content-Length": str(end - start + 1),
                "Content-Range": f"bytes {start}-{end}/{len(payload)}",
            })
        if self.mode == "oversized_response":
            payload = b"x" * (staging_smoke.MAX_RESPONSE_BYTES + 1)
        if self.mode == "corrupt_bytes":
            payload = b"wrong fixture bytes"
        return respond(200, content=payload, headers={"ETag": "synthetic"})


def run_smoke(arguments, monkeypatch, mode="success"):
    service = FixtureService(mode)
    monkeypatch.setattr(staging_smoke.httpx, "Client", service.client)
    monkeypatch.setattr(staging_smoke.time, "sleep", lambda seconds: None)
    result = staging_smoke.main(arguments)
    report = json.loads(Path(arguments[arguments.index("--output") + 1]).read_text())
    return result, report, service


def test_complete_mock_workflow_is_scoped_and_sanitized(arguments, monkeypatch, capsys):
    result, report, service = run_smoke(arguments, monkeypatch)
    assert result == 0
    assert report["status"] == "passed"
    assert report["production_qualified"] is report["release_qualified"] is False
    assert report["bindings"] == {
        "environment_id": "staging-01", "candidate_id": "candidate-01",
        "configuration_id": "config-01",
    }
    assert report["cleanup"] == {"status": "passed", "objects_created": 4, "objects_removed": 4,
                                  "uncertain_writes": 0, "unsettled_jobs": 0}
    assert len(service.jobs) == 6
    assert {UUID(job["job_id"]) for job in report["jobs"]} == {
        UUID(job_id) for job_id in service.jobs
    }
    assert {job["status"] for job in report["jobs"]} == {"succeeded"}
    assert not service.objects and not service.records
    assert len(service.deleted) == 4
    assert all(key.startswith(report["synthetic_prefix"]) for _, _, key in service.deleted)
    names = {check["name"] for check in report["checks"] if check["passed"]}
    assert "tls.unrelated_ca_rejected" in names
    assert "tenant_1.hot.scan.published_sha256" in names
    assert "tenant_2.policy.placement_published" in names
    serialized = json.dumps(report) + capsys.readouterr().out
    for private in (*TOKENS, PRIVATE_ENDPOINT, PRIVATE_EXCEPTION, "smoke-fixtures"):
        assert private not in serialized


@pytest.mark.parametrize("mode,check,mutates", [
    ("public_metrics", "metrics.private.status", False),
    ("anonymous_access", "auth.missing.status", False),
    ("unrelated_ca_accepted", "tls.unrelated_ca_rejected", False),
    ("unrelated_network_failure", "tls.unrelated_ca_rejected", False),
    ("plaintext_accepted", "tls.plaintext.status", False),
    ("bearer_redirect", "tenant_1.empty_prefix.status", False),
    ("occupied_prefix", "tenant_1.prefix_unused", False),
    ("tenant_alias", "hot.isolation_absent.status", True),
    ("corrupt_bytes", "tenant_1.hot.isolated.exact_bytes", True),
    ("oversized_response", "tenant_1.hot.isolated.get.response_bound", True),
    ("scan_without_effect", "tenant_1.hot.scan.published_sha256", True),
    ("policy_without_effect", "tenant_1.policy.moved.head.status", True),
    ("job_failed", "tenant_1.hot.scan.succeeded", True),
    ("job_url_redirect", "tenant_1.hot.scan.contract", True),
    ("job_never_terminal", "tenant_1.hot.scan.poll_limit", True),
])
def test_failures_fail_closed_without_raw_details(arguments, monkeypatch, capsys, mode, check, mutates):
    result, report, service = run_smoke(arguments, monkeypatch, mode)
    assert result == 1
    assert report["status"] == "failed"
    assert report["failure"] == {"check": check}
    assert bool([request for _, request in service.requests if request.method == "PUT"]) == mutates
    assert all(request.url.host == "private-staging.invalid" for _, request in service.requests)
    output = json.dumps(report) + capsys.readouterr().out
    assert PRIVATE_EXCEPTION not in output
    assert all(token not in output for token in TOKENS)
    if mode in ("job_url_redirect", "job_never_terminal"):
        assert report["cleanup"]["status"] == "deferred_unsettled_jobs"
        assert not service.deleted
        assert report["jobs"][0]["job_id"] in service.jobs
        assert report["jobs"][0]["status"] in ("unknown", "queued")
    elif mutates:
        assert not service.objects


def test_cleanup_failure_changes_success_to_failed(arguments, monkeypatch):
    result, report, service = run_smoke(arguments, monkeypatch, "cleanup_failure")
    assert result == 1
    assert report["status"] == "failed"
    assert report["cleanup"]["status"] == "failed"
    assert report["cleanup"]["objects_removed"] == 0
    assert len(service.objects) == 4
    assert len([request for _, request in service.requests if request.method == "DELETE"]) == 4


def test_ambiguous_put_never_deletes_unconfirmed_ownership(arguments, monkeypatch):
    result, report, service = run_smoke(arguments, monkeypatch, "uncertain_write")
    assert result == 1
    assert report["cleanup"]["status"] == "uncertain_writes_require_reconciliation"
    assert report["cleanup"]["uncertain_writes"] == 1
    assert len(service.objects) == 1
    assert not service.deleted


@pytest.mark.parametrize("flag,value", [
    ("--base-url", "http://private-staging.invalid"),
    ("--base-url", "https://user:secret@private-staging.invalid"),
    ("--base-url", "https://private-staging.invalid/?credential=private"),
    ("--plaintext-url", "http://other-host.invalid"),
    ("--bucket", "../unsafe-bucket"),
    ("--environment-id", "https://private.invalid"),
    ("--request-timeout", "0"),
    ("--deadline-seconds", "999999"),
    ("--poll-interval", "0"),
    ("--tenant-b-token-file", "/does-not-exist/credential"),
])
def test_invalid_inputs_make_no_requests(arguments, monkeypatch, flag, value):
    if flag in arguments:
        arguments[arguments.index(flag) + 1] = value
    else:
        arguments.extend([flag, value])
    result, report, service = run_smoke(arguments, monkeypatch)
    assert result == 1
    assert report["status"] == "failed"
    assert report["http_request_count"] == 0
    assert not service.requests


def test_opt_in_required_and_no_output_overwrite(arguments, monkeypatch):
    arguments.remove("--run-staging-smoke")
    result, report, service = run_smoke(arguments, monkeypatch)
    assert result == 1
    assert report["failure"] == {"check": "inputs.explicit_opt_in"}
    assert not service.requests
    output = Path(arguments[arguments.index("--output") + 1])
    before = output.read_bytes()
    assert staging_smoke.main(arguments) == 2
    assert output.read_bytes() == before


def test_identical_tokens_rejected_before_network(arguments, monkeypatch):
    arguments[arguments.index("--tenant-b-token-file") + 1] = arguments[
        arguments.index("--tenant-a-token-file") + 1
    ]
    result, report, service = run_smoke(arguments, monkeypatch)
    assert result == 1
    assert report["failure"] == {"check": "inputs.distinct_tokens"}
    assert not service.requests


def test_token_files_never_overwritten_by_report(arguments, monkeypatch):
    token_file = Path(arguments[arguments.index("--tenant-a-token-file") + 1])
    arguments[arguments.index("--output") + 1] = str(token_file)
    called = []
    monkeypatch.setattr(staging_smoke.httpx, "Client", lambda **kwargs: called.append(kwargs))
    assert staging_smoke.main(arguments) == 2
    assert token_file.read_text() == TOKENS[0]
    assert not called


def test_retained_candidate_and_digest_identifiers_are_supported(arguments, monkeypatch):
    candidate = "0.1.1rc1-e51cb39cc84b-1f065e90bbcb"
    configuration = f"sha256:{'a' * 64}"
    arguments[arguments.index("--candidate-id") + 1] = candidate
    arguments[arguments.index("--configuration-id") + 1] = configuration
    result, report, _ = run_smoke(arguments, monkeypatch)
    assert result == 0
    assert report["bindings"]["candidate_id"] == candidate
    assert report["bindings"]["configuration_id"] == configuration


def test_deadline_checked_for_each_unbuffered_response_chunk(arguments, monkeypatch):
    checks = staging_smoke.Checks()
    config = staging_smoke.settings(staging_smoke.parser().parse_args(arguments), checks)
    now = [0.0]
    monkeypatch.setattr(staging_smoke.time, "monotonic", lambda: now[0])
    smoke = staging_smoke.Smoke(config, checks, uuid4().hex)
    smoke.deadline = 5
    chunks = []

    class TrickleStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(100):
                now[0] += 1
                chunks.append(b"x")
                yield b"x"

    with HTTPX_CLIENT(base_url=config.origin, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=TrickleStream()),
    )) as client:
        with pytest.raises(staging_smoke.SmokeFailure, match="test.trickle.deadline"):
            smoke.request(client, "test.trickle", "GET", "/healthz", 200)
    assert len(chunks) == 5
