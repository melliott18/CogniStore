#!/usr/bin/env python3
"""Live-cluster acceptance assertions; use verify.sh to provision the fixture."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

NAMESPACE = "cognistore-acceptance"
NAME = "acceptance-cognistore"
BUCKET = "helm-acceptance"
PAYLOAD = b"CogniStore Helm persistent object invariant\n"


def kubectl(*args, stdin=None):
    return subprocess.check_output(
        ["kubectl", "--namespace", NAMESPACE, *args], input=stdin, text=True,
    )


def request(base, path, *, payload=None, method="GET", raw=False, timeout=30):
    headers = {}
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=payload, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read()
    return body if raw else json.loads(body)


def wait_jobs(base, jobs, timeout=600):
    pending = set(jobs)
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        for job_id in tuple(pending):
            status = request(base, f"/v1/jobs/{job_id}")
            assert status["status"] not in {"failed", "dead_lettered"}, status
            if status["status"] == "succeeded":
                assert status["attempt"] == 1, status
                pending.remove(job_id)
        if pending:
            time.sleep(2)
    assert not pending, f"Jobs did not finish: {sorted(pending)}"


def audit_snapshot(jobs, *, completed=True):
    # Read persisted delivery events, so a duplicate success cannot be hidden by
    # the API's projection of the most recent job status.
    code = """
import json, os
from collections import Counter
from cognistore.core.audit import AuditQuery
from cognistore.db import open_catalog
catalog = open_catalog(os.environ['COGNISTORE_CATALOG_DB'], migrate=False)
try:
    result = {}
    for job_id in JOB_IDS:
        events = catalog.list_audit_events(AuditQuery(job_id=job_id))
        counts = Counter(event.event_type for event in events)
        assert counts['job.queued'] == 1, (job_id, counts)
        assert counts['job.started'] == EXPECTED_EXECUTIONS, (job_id, counts)
        assert counts['job.succeeded'] == EXPECTED_EXECUTIONS, (job_id, counts)
        assert not any(counts[key] for key in ('job.failure', 'job.retry_scheduled', 'job.dead_lettered')), (job_id, counts)
        result[job_id] = sorted(event.event_id for event in events)
    print(json.dumps(result))
finally:
    catalog.close()
""".replace("JOB_IDS", repr(jobs)).replace("EXPECTED_EXECUTIONS", str(int(completed)))
    return json.loads(kubectl("exec", "-i", f"deployment/{NAME}-api", "--", "python", "-", stdin=code))


def schedule_snapshot():
    code = """
import json, sqlite3
with sqlite3.connect('file:/var/lib/cognistore/schedule/schedule.sqlite3?mode=ro', uri=True) as db:
    rows = db.execute('SELECT scope, schedule_id, next_run_at FROM scheduled_jobs ORDER BY scope').fetchall()
    assert len(rows) == 1 and rows[0][1] == 'acceptance-disabled', rows
    print(json.dumps(rows))
"""
    return json.loads(kubectl("exec", "-i", f"deployment/{NAME}-scheduler", "--", "python", "-", stdin=code))


def seed(base, output):
    assert request(base, "/healthz")["status"] == "ok"
    assert b"<html" in request(base, "/ui/", raw=True).lower()
    assert b"cognistore_http_requests_total" in request(base, "/metrics", raw=True)
    for index in range(64):
        request(base, f"/v1/objects/hot/{BUCKET}/{index}.txt", payload=PAYLOAD, method="PUT")
    job = request(base, "/v1/actions/catalog-scans", method="POST", payload={
        "tier": "hot", "bucket": BUCKET,
    })
    wait_jobs(base, [job["job_id"]])
    pause_workers()
    queued = request(base, "/v1/actions/catalog-scans", method="POST", payload={
        "tier": "hot", "bucket": BUCKET,
    })
    result = {
        "catalog": request(base, f"/v1/catalog/objects/{BUCKET}/0.txt"),
        "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
        "job": request(base, job["status_url"]),
        "audit": audit_snapshot([job["job_id"]]),
        "schedule": schedule_snapshot(),
        "queued_job": queued,
        "queued_audit": audit_snapshot([queued["job_id"]], completed=False),
    }
    output.write_text(json.dumps(result, indent=2) + "\n")
    print("Clean install: API, UI, object persistence and real worker job passed", flush=True)


def read_after_restart(base, path, output, *, timeout=120, clock=time.monotonic, sleep=time.sleep):
    """Bound recovery after an intentional backend restart; never retry writes.

    A TCP-ready replacement pod does not prove that existing SDK connections
    have recovered. Record every transient read failure, then let verify check
    the first returned payload and all original persistence invariants.
    """
    started = clock()
    attempts = []
    status = "failed"
    try:
        while clock() - started < timeout:
            remaining = timeout - (clock() - started)
            if remaining <= 0:
                break
            attempt = {"elapsed_seconds": clock() - started}
            attempts.append(attempt)
            try:
                body = request(base, path, raw=True, timeout=min(10, remaining))
            except urllib.error.HTTPError as exc:
                attempt["http_status"] = exc.code
                if exc.code not in {502, 503, 504}:
                    raise
            except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
                attempt["error_type"] = type(exc).__name__
            else:
                if clock() - started >= timeout:
                    raise TimeoutError("backend response exceeded the acceptance deadline")
                status = "response_received"
                return body
            sleep(min(1, max(0, timeout - (clock() - started))))
        raise TimeoutError("backend read did not recover within the acceptance deadline")
    finally:
        output.write_text(json.dumps({"status": status, "deadline_seconds": timeout,
                                      "elapsed_seconds": clock() - started,
                                      "attempts": attempts}, indent=2) + "\n")


def verify(base, output, *, recover_backends=False):
    before = json.loads(output.read_text())
    path = f"/v1/objects/hot/{BUCKET}/0.txt"
    body = (read_after_restart(base, path, output.with_name("backend-recovery.json"))
            if recover_backends else request(base, path, raw=True))
    assert hashlib.sha256(body).hexdigest() == before["sha256"]
    catalog = request(base, f"/v1/catalog/objects/{BUCKET}/0.txt")
    for field in ("bucket", "key", "tier", "size"):
        assert catalog[field] == before["catalog"][field], (field, catalog)
    assert request(base, before["job"]["status_url"]) == before["job"]
    assert audit_snapshot([before["job"]["job_id"]]) == before["audit"]
    assert schedule_snapshot() == before["schedule"]
    assert request(base, before["queued_job"]["status_url"]) == before["queued_job"]
    assert audit_snapshot([before["queued_job"]["job_id"]], completed=False) == before["queued_audit"]
    print("Catalog, object checksum, completed job and delivery audit survived", flush=True)


def replicas(component):
    deployment = json.loads(kubectl("get", "deployment", f"{NAME}-{component}", "-o", "json"))
    return deployment["spec"]["replicas"], deployment.get("status", {}).get("readyReplicas", 0)


def await_replicas(component, predicate, *, timeout=240, evidence=None, phase=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        desired, ready = replicas(component)
        if evidence is not None:
            evidence.append({"component": component, "phase": phase, "time": time.time(),
                             "desired": desired, "ready": ready})
        if predicate(desired, ready):
            return
        time.sleep(2)
    raise AssertionError(f"{component} replicas failed acceptance condition: {replicas(component)}")


def pause_workers():
    kubectl("annotate", "scaledobject", f"{NAME}-worker", "autoscaling.keda.sh/paused-replicas=0", "--overwrite")
    await_replicas("worker", lambda desired, ready: desired == 0 and ready == 0)
    # A terminating pod disappears from readyReplicas before its process exits.
    # Wait for deletion before publishing the durable backlog.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        pods = json.loads(kubectl("get", "pods", "-l",
                                 "app.kubernetes.io/instance=acceptance,app.kubernetes.io/component=worker", "-o", "json"))
        if not pods["items"]:
            return
        time.sleep(1)
    raise AssertionError("Workers did not finish termination before queue seeding")


def autoscale(base, output, invariants=None):
    evidence = []
    stop = threading.Event()
    # Seeding/rollout verification also creates HTTP traffic. Let that decay so
    # the acceptance test proves an actual scale transition caused by its load.
    await_replicas("api", lambda desired, ready: desired == ready == 1,
                   evidence=evidence, phase="before_load")

    def api_load():
        count = 0
        while not stop.is_set():
            request(base, f"/v1/catalog/objects/{BUCKET}/0.txt")
            count += 1
        return count

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(api_load) for _ in range(6)]
        try:
            await_replicas("api", lambda desired, ready: desired >= 2 and ready >= 2,
                           evidence=evidence, phase="request_load")
        finally:
            stop.set()
        requests = sum(future.result() for future in futures)
    print(f"Request metric HPA scaled API to multiple ready replicas ({requests} requests)", flush=True)

    # Pause at zero only while seeding, to get a reproducible real queue backlog.
    # Unpausing restores the chart's minReplicas=1 and lets KEDA/HPA select scale.
    pause_workers()
    jobs = []
    try:
        for _ in range(250):
            job = request(base, "/v1/actions/catalog-scans", method="POST", payload={
                "tier": "hot", "bucket": BUCKET,
            })
            jobs.append(job["job_id"])
    finally:
        kubectl("annotate", "scaledobject", f"{NAME}-worker", "autoscaling.keda.sh/paused-replicas-")
    await_replicas("worker", lambda desired, ready: desired >= 2 and ready >= 2,
                   evidence=evidence, phase="queue_load")
    wait_jobs(base, jobs)
    if invariants is not None:
        # The job queued before the Helm upgrade must survive every rollout and
        # dependency restart, then be delivered once when workers resume.
        preserved_job = json.loads(invariants.read_text())["queued_job"]["job_id"]
        wait_jobs(base, [preserved_job])
        jobs.append(preserved_job)
    audits = audit_snapshot(jobs)
    await_replicas("api", lambda desired, ready: desired == ready == 1,
                   evidence=evidence, phase="after_load")
    await_replicas("worker", lambda desired, ready: desired == ready == 1,
                   evidence=evidence, phase="after_load")
    output.write_text(json.dumps({"requests": requests, "jobs": len(jobs), "replicas": evidence,
                                  "audit": audits}, indent=2) + "\n")
    print(f"JetStream backlog scaled workers; {len(jobs)} jobs each ran and succeeded exactly once", flush=True)


def security():
    pods = json.loads(kubectl("get", "pods", "-l", "app.kubernetes.io/instance=acceptance", "-o", "json"))
    assert pods["items"]
    for pod in pods["items"]:
        spec = pod["spec"]
        assert spec.get("automountServiceAccountToken") is False
        assert spec["securityContext"]["runAsNonRoot"] is True
        assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        for container in spec["containers"]:
            context = container["securityContext"]
            assert context["allowPrivilegeEscalation"] is False
            assert context["readOnlyRootFilesystem"] is True
            assert "ALL" in context["capabilities"]["drop"]
    for component in ("api", "worker", "scheduler"):
        kubectl("exec", f"deployment/{NAME}-{component}", "--", "python", "-c",
                "import os; assert os.getuid() != 0; assert not os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/token')")
    print("Live API and workers run non-root without Kubernetes API credentials", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["seed", "verify", "autoscale", "security"])
    parser.add_argument("--base-url", help="URL printed by this test's live kubectl port-forward")
    parser.add_argument("--output", type=Path, default=Path("test-results/kubernetes-invariants.json"))
    parser.add_argument("--invariants", type=Path, help="seed snapshot containing the preserved queued job")
    parser.add_argument("--recover-backends", action="store_true",
                        help="record bounded read recovery after deliberately restarting dependencies")
    args = parser.parse_args()
    if args.recover_backends and args.mode != "verify":
        parser.error("--recover-backends requires verify")
    if args.mode != "security" and args.base_url is None:
        parser.error("--base-url must identify the test's live kubectl port-forward")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.mode == "security":
        security()
    elif args.mode == "autoscale":
        autoscale(args.base_url, args.output, args.invariants)
    elif args.mode == "verify":
        verify(args.base_url, args.output, recover_backends=args.recover_backends)
    else:
        globals()[args.mode](args.base_url, args.output)


if __name__ == "__main__":
    main()
