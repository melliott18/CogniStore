"""Actual local SQLite/POSIX application checks; no staging qualification claim."""
from __future__ import annotations

import asyncio
import json
import ssl
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from scripts.load_corpus import DOCX, OPAQUE, PDF, TENANTS, ObjectSpec, iter_object_specs
from scripts.load_service import LoadService, ServiceFailure, ServiceSettings
from scripts.load_workload import Request
from scripts.manual_acceptance_local import build_app


@pytest.fixture
def ca(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "load-local-fixture")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256()))
    path = tmp_path / "ca.pem"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path


@pytest.fixture
def config(tmp_path, ca):
    tokens = {}
    for tenant in TENANTS:
        tokens[tenant] = tmp_path / f"{tenant}.token"
        tokens[tenant].write_text(f"private-{tenant}-token")
    return ServiceSettings("https://load-fixture.invalid", ca, tokens, "load-fixture", "fixture-run-0001")


def specs():
    return [ObjectSpec(tenant, f"corpus/{index:08d}", 4096, OPAQUE)
            for tenant in TENANTS for index in range(10)]


def request(operation, *, tenant="pilot-a", put_kind=None, hotspot=True, selection=0):
    return Request(0, operation, tenant, 4096, put_kind, hotspot, selection)


@pytest.fixture
def local(tmp_path, config):
    root = tmp_path / "application"
    app = build_app(root)
    tokens = json.loads((root / "tokens.private.json").read_text())
    for tenant in TENANTS:
        config.token_files[tenant].write_text(tokens[f"{tenant}-admin"])
    return app, tokens


def test_actual_application_operations_reconcile_and_cleanup(tmp_path, config, local):
    app, _ = local

    async def exercise():
        async with LoadService(config, specs(), tmp_path / "journal.jsonl",
                               transport=httpx.ASGITransport(app=app)) as service:
            await service.preflight()
            seeded = await service.seed()
            assert seeded["total_objects"] == 20
            for operation in ("get", "head", "catalog", "ask", "policy_preview"):
                assert (await service(request(operation))).success, operation
            entry = service._pools[("pilot-a", 4096, True, False)][0]
            initial_hash = entry.sha256
            assert (await service(request("put", put_kind="replace"))).success
            assert entry.sha256 != initial_hash
            assert (await service(request("put", put_kind="create"))).success
            assert service.inventory()["total_objects"] == 21
            assert (await service(request("delete"))).success
            assert (await service.normalize_churn())["total_objects"] == 20
            assert all(entry.present == entry.initial for entry in service.entries)
            report = await service.reconcile()
            assert report["status"] == "passed"
            assert report["verified_objects"] == 20
            assert report["snapshots"]["pilot-a"]["audit_valid"]
            assert (await service.cleanup())["removed_objects"] == 20
            assert service.inventory()["total_objects"] == 0

    asyncio.run(exercise())
    journal = (tmp_path / "journal.jsonl").read_text()
    assert "Bearer" not in journal
    assert config.origin not in journal
    assert "audit_baseline" in journal


def test_exact_initial_corpus_and_job_size_mix(tmp_path, config):
    service = LoadService(config, iter_object_specs(100000), tmp_path / "journal.jsonl")
    assert sum(e.initial for e in service.entries) == 100000
    assert sum(e.initial and e.churn for e in service.entries) == 800
    for prefix in service.movement_prefixes():
        entries = [e for e in service.entries if e.key.startswith(prefix)]
        assert len(entries) == 200
        assert {size: sum(e.spec.size_bytes == size for e in entries)
                for size in (4096, 65536, 1048576, 16777216)} == {
                    4096: 120, 65536: 60, 1048576: 18, 16777216: 2}
    assert len(service.movement_prefixes()) >= 50
    for (tenant, size, hot, churn), pool in service._pools.items():
        if not hot:
            hot_pool = service._pools[(tenant, size, True, churn)]
            assert len(pool) == len(hot_pool) * 4


def test_token_rotation_and_expiry_real_application(tmp_path, config, local):
    app, tokens = local

    async def exercise():
        async with LoadService(config, specs(), tmp_path / "journal.jsonl",
                               transport=httpx.ASGITransport(app=app)) as service:
            await service.preflight()
            await service.seed()
            config.token_files["pilot-a"].write_text(tokens["pilot-a-expired"])
            assert (await service(request("get"))).outcome == "http_error"
            config.token_files["pilot-a"].write_text(tokens["pilot-a-admin"])
            assert (await service(request("get"))).success

    asyncio.run(exercise())


def test_extra_catalog_object_is_detected(tmp_path, config, local):
    app, _ = local

    async def exercise():
        async with LoadService(config, specs(), tmp_path / "journal.jsonl",
                               transport=httpx.ASGITransport(app=app)) as service:
            await service.preflight()
            await service.seed()
            await service._request("pilot-a", "PUT",
                f"/v1/objects/hot/{config.bucket}/{service.prefix}unexpected", (201,), content=b"extra")
            report = await service.reconcile()
            assert report["status"] == "failed"
            assert report["catalog_mismatches"] == 1
            with pytest.raises(ServiceFailure, match="cleanup_not_permitted"):
                await service.cleanup()

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["disconnect", "cancel", "bad_response", "redirect"])
def test_uncertain_write_is_never_retried_or_cleaned(tmp_path, config, local, mode):
    app, _ = local
    asgi = httpx.ASGITransport(app=app)
    puts = 0

    async def handler(req):
        nonlocal puts
        if req.method == "PUT":
            puts += 1
            if mode == "cancel":
                raise asyncio.CancelledError()
            if mode == "disconnect":
                raise httpx.ReadError("private endpoint token response")
            if mode == "redirect":
                return httpx.Response(307, headers={"Location": "https://private-other.invalid"})
            return httpx.Response(201, json={"not": "the required object"})
        return await asgi.handle_async_request(req)

    async def exercise():
        async with LoadService(config, specs(), tmp_path / "journal.jsonl",
                               transport=httpx.MockTransport(handler)) as service:
            await service.preflight()
            with pytest.raises((ServiceFailure, httpx.ReadError, asyncio.CancelledError)):
                await service.seed()
            assert puts == 1
            assert service.stopped
            assert service.uncertain_count == 1
            assert not (await service(request("put", put_kind="create"))).success
            with pytest.raises(ServiceFailure, match="cleanup_not_permitted"):
                await service.cleanup()

    asyncio.run(exercise())
    content = (tmp_path / "journal.jsonl").read_text()
    assert "mutation_intent" in content and "mutation_uncertain" in content
    assert "private endpoint" not in content and "private-other" not in content


def test_bounded_response_and_request_deadline(tmp_path, config):
    async def handler(req):
        if req.url.path == "/slow":
            await asyncio.sleep(10)
        return httpx.Response(200, content=b"x" * 20)

    async def exercise():
        short = ServiceSettings(config.origin, config.ca_file, config.token_files, config.bucket,
                                config.run_id, request_timeout=0.01)
        async with LoadService(short, specs(), tmp_path / "journal.jsonl",
                               transport=httpx.MockTransport(handler)) as service:
            with pytest.raises(ServiceFailure, match="response_too_large"):
                await service._request(None, "GET", "/large", max_bytes=10)
            with pytest.raises(httpx.TimeoutException):
                await service._request(None, "GET", "/slow")

    asyncio.run(exercise())


def test_client_security_configuration(tmp_path, config, monkeypatch):
    actual = httpx.AsyncClient
    options = []

    def capture(**kwargs):
        options.append(kwargs)
        return actual(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", capture)

    async def exercise():
        async with LoadService(config, specs(), tmp_path / "journal.jsonl"):
            pass

    asyncio.run(exercise())
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False
    assert options[0]["verify"].verify_mode == ssl.CERT_REQUIRED
    assert options[0]["verify"].check_hostname


@pytest.mark.parametrize("origin", ["http://local.test", "https://secret@local.test", "https://local.test/path"])
def test_rejects_unsafe_origin(config, origin):
    with pytest.raises(ValueError):
        ServiceSettings(origin, config.ca_file, config.token_files, config.bucket, config.run_id)


def test_real_api_jobs_execute_local_scanning_and_movement(tmp_path, config, local):
    """Actual handlers with an in-process queue; NATS delivery is outside this test."""
    from types import SimpleNamespace

    from cognistore.core.audit import AuditEventType, AuditOutcome
    from cognistore.core.mime_detection import LibmagicMimeDetector, MimeDetectorUnavailableError
    from cognistore.jobs.handlers import build_handlers
    from cognistore.jobs.models import JobContext
    try:
        LibmagicMimeDetector()
    except MimeDetectorUnavailableError:
        pytest.skip("native libmagic required for actual scan MIME/extraction validation")
    app, _ = local
    gateway = app.state.gateway
    handlers = build_handlers(gateway._base_drivers, gateway._catalog)
    accepted = []

    class LocalQueue:
        async def probe(self):
            return SimpleNamespace(ready=True)

        async def enqueue(self, job, **kwargs):
            accepted.append(job)
            context = JobContext(1, False, 1, 1, asyncio.Event())
            await handlers[job.job_type](job, context)
            await gateway._append_status_event(job, AuditEventType.JOB_SUCCEEDED,
                                               AuditOutcome.SUCCEEDED, details={"attempt": 1})

    gateway.queue = LocalQueue()

    async def exercise():
        async with LoadService(config, [ObjectSpec(e.tenant, e.key, e.size_bytes,
                PDF if e.key.endswith("08") else DOCX if e.key.endswith("09") else OPAQUE)
                for e in specs()], tmp_path / "journal.jsonl",
                               transport=httpx.ASGITransport(app=app)) as service:
            await service.preflight()
            await service.seed()
            scanned = await service.background_once("catalog.scan", 0)
            assert len(scanned) == 2
            assert service.inventory()["scanned_objects"] == 20
            for entry in service.entries:
                if entry.present and entry.spec.content_type in (PDF, DOCX):
                    metadata = gateway._catalog.for_tenant(entry.spec.tenant).get(
                        config.bucket, entry.key).metadata
                    assert metadata["document_extraction"]["status"] == "succeeded"
                    assert metadata["mime_detection"]["detector"] == "libmagic"
            assert (await service(request("put", put_kind="create"))).success
            moved = await service.move_all("hot-to-warm")
            assert moved["verified_moves"] == 21
            assert all(e.tier == "warm" for e in service.entries if e.present)
            assert (await service(request("get"))).success
            assert (await service.move_all("warm-to-hot"))["verified_moves"] == 21
            assert all(e.tier == "hot" for e in service.entries if e.present)
            assert (await service.reconcile())["status"] == "passed"
            assert len(accepted) == 10

    asyncio.run(exercise())


def test_unsettled_job_stops_without_resubmission(tmp_path, config, local):
    app, _ = local
    asgi = httpx.ASGITransport(app=app)
    submitted = 0
    from uuid import uuid4
    job_id = str(uuid4())

    async def handler(req):
        nonlocal submitted
        if req.url.path.startswith("/v1/actions/"):
            submitted += 1
            return httpx.Response(202, headers={"Location": f"/v1/jobs/{job_id}"}, json={
                "job_id": job_id, "job_type": "policy.run", "status_url": f"/v1/jobs/{job_id}"})
        if req.url.path.startswith("/v1/jobs/"):
            return httpx.Response(200, json={"job_id": job_id, "job_type": "policy.run", "status": "running"})
        return await asgi.handle_async_request(req)

    async def exercise():
        settings = ServiceSettings(config.origin, config.ca_file, config.token_files, config.bucket,
                                   config.run_id, job_timeout=0.05, poll_interval=0.01)
        async with LoadService(settings, specs(), tmp_path / "journal.jsonl",
                               transport=httpx.MockTransport(handler)) as service:
            await service.preflight()
            await service.seed()
            with pytest.raises(asyncio.TimeoutError):
                await service.run_job("pilot-a", "policy.run", service.job_prefixes("policy.run")[0])
            assert submitted == 1
            assert service.unsettled_jobs == 1
            assert service.stopped
            with pytest.raises(ServiceFailure, match="cleanup_not_permitted"):
                await service.cleanup()

    asyncio.run(exercise())


@pytest.mark.parametrize("missing", [None, "publication", "attempt", "terminal"])
def test_native_job_export_retains_retries_and_refuses_gaps(tmp_path, config, missing):
    from uuid import uuid4

    job_id = str(uuid4())
    published = "2026-09-29T01:00:00+00:00"
    events = []
    for number, event_type, minute in ((1, "job.started", 1), (1, "job.failure", 2),
                                      (2, "job.started", 3), (2, "job.succeeded", 4)):
        events.append({"event_id": str(uuid4()), "event_type": event_type,
                       "job_id": job_id, "occurred_at": f"2026-09-29T01:0{minute}:00+00:00",
                       "details": {"cumulative_attempt": number, "job_type": "catalog.scan",
                                   "source_published_at": published}})
    if missing == "publication":
        events[0]["details"].pop("source_published_at")
    elif missing == "attempt":
        events = events[2:]
    elif missing == "terminal":
        events.pop()

    async def handle(req):
        assert req.url.params["job_id"] == job_id
        return httpx.Response(200, json={"items": [row for row in events
            if row["event_type"] == req.url.params["event_type"]], "page": {"next_cursor": None}})

    async def exercise():
        async with LoadService(config, specs(), tmp_path / "journal.jsonl",
                               transport=httpx.MockTransport(handle)) as service:
            service._jobs[job_id] = {"job_id": job_id, "tenant": "pilot-a",
                                     "kind": "catalog.scan", "object_count": 100}
            report = await service.export_job_observations()
            if missing:
                assert report["gaps"] and not report["jobs"] and not report["scans"]
            else:
                assert not report["gaps"]
                assert report["jobs"][0]["published_at"] == published
                assert report["jobs"][0]["accepted_at"] == events[0]["occurred_at"]
                assert report["jobs"][0]["completed_at"] == events[-1]["occurred_at"]
                assert report["jobs"][0]["attempt_count"] == 2
                assert [row["success"] for row in report["scans"]] == [False, True]
                assert all(row["published_at"] == published for row in report["scans"])

    asyncio.run(exercise())



def test_full_profile_churn_covers_descriptor_cycle_and_inflight_headroom(tmp_path, config):
    from collections import Counter

    from scripts.load_workload import request_for_sequence

    service = LoadService(config, iter_object_specs(100000), tmp_path / "journal.jsonl")
    capacity = Counter()
    occupied = Counter()
    initial = Counter()
    for entry in service.entries:
        if entry.churn:
            key = (entry.spec.tenant, entry.spec.size_bytes, entry.hotspot)
            capacity[key] += 1
            occupied[key] += entry.initial
            initial[key] += entry.initial
    minimum = occupied.copy()
    maximum = occupied.copy()
    for sequence in range(100000):
        row = request_for_sequence(sequence)
        key = (row.tenant, row.size_bytes, row.hotspot)
        if row.put_kind == "create":
            assert occupied[key] < capacity[key]
            occupied[key] += 1
        elif row.operation == "delete":
            assert occupied[key] > 0
            occupied[key] -= 1
        minimum[key] = min(minimum[key], occupied[key])
        maximum[key] = max(maximum[key], occupied[key])
    # Four concurrent PUTs can finish before earlier DELETEs (or vice versa).
    assert all(minimum[key] >= 4 and capacity[key] - maximum[key] >= 4 for key in capacity)
    assert sum(occupied.values()) == sum(initial.values())


def test_background_scans_one_nonempty_tier_per_tenant_and_rotates_mixed_scopes(tmp_path, config, local):
    from types import SimpleNamespace

    from cognistore.core.audit import AuditEventType, AuditOutcome
    from cognistore.core.mime_detection import LibmagicMimeDetector, MimeDetectorUnavailableError
    from cognistore.jobs.handlers import build_handlers
    from cognistore.jobs.models import JobContext

    try:
        LibmagicMimeDetector()
    except MimeDetectorUnavailableError:
        pytest.skip("native libmagic required for actual scan validation")
    app, _ = local
    gateway = app.state.gateway
    handlers = build_handlers(gateway._base_drivers, gateway._catalog)
    accepted = []

    class LocalQueue:
        async def probe(self):
            return SimpleNamespace(ready=True)

        async def enqueue(self, job, **kwargs):
            accepted.append(job)
            await handlers[job.job_type](job, JobContext(1, False, 1, 1, asyncio.Event()))
            await gateway._append_status_event(job, AuditEventType.JOB_SUCCEEDED,
                                               AuditOutcome.SUCCEEDED, details={"attempt": 1})

    gateway.queue = LocalQueue()

    async def exercise():
        async with LoadService(config, specs(), tmp_path / "journal.jsonl",
                               transport=httpx.ASGITransport(app=app)) as service:
            await service.preflight()
            await service.seed()
            prefixes = service.job_prefixes("catalog.scan")
            empty = next(prefix for prefix in prefixes if "/churn/" in prefix)
            with pytest.raises(ServiceFailure, match="scan_scope_empty"):
                await service.run_job("pilot-a", "catalog.scan", empty, tier=None)
            assert accepted == []
            assert service.unsettled_jobs == 0
            assert not service.stopped

            # Actual application policy jobs create a mixed-placement prefix.
            # Refresh the adapter cache as its tracked jobs ordinarily do.
            for tenant in TENANTS:
                for entry in [e for e in service.entries if e.spec.tenant == tenant and e.initial][:2]:
                    await service._request(tenant, "POST", "/v1/actions/policy-runs", (202,), json={
                        "bucket": config.bucket, "prefix": entry.key,
                        "config": {"policy": "simple", "threshold": 1, "allowed_tiers": ["hot", "warm"]}})
                    await service._catalog(entry)
            accepted.clear()
            first = await service.background_once("catalog.scan", 0)
            assert len(first) == 2 and [row["verified_objects"] for row in first] == [8, 8]
            assert [job.payload["tier"] for job in accepted] == ["hot", "hot"]
            assert service.inventory()["scanned_objects"] == 16
            second = await service.background_once("catalog.scan", 0)
            assert len(second) == 2 and [row["verified_objects"] for row in second] == [2, 2]
            assert [job.payload["tier"] for job in accepted[2:]] == ["warm", "warm"]
            assert service.inventory()["scanned_objects"] == 20
            assert sorted(row["object_count"] for row in service._jobs.values()) == [2, 2, 8, 8]

            # An empty rotating prefix is skipped before any POST; exactly one
            # nonempty scan is still offered for each tenant in this period.
            third = await service.background_once("catalog.scan", prefixes.index(empty))
            assert len(third) == 2 and len(accepted) == 6
            assert all(row["object_count"] > 0 for row in service._jobs.values())
            assert (await service.reconcile())["status"] == "passed"

    asyncio.run(exercise())
    assert "scan_empty_prefix_skipped" in (tmp_path / "journal.jsonl").read_text()
