"""Review completed qualification runs and retain explicit scope/skip identities."""

import json
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
source = sys.argv[2]
checkout = sys.argv[3]
expected = {
    c["context"]
    for c in json.load(open("docs/evidence/ci-158/remediation/protection-request.json"))[
        "required_status_checks"
    ]["checks"]
}
runs = [json.loads(p.read_text()) for p in sorted(root.glob("*/run.json"))]
assert len(runs) == 3
assert all(
    r["headSha"] == source and r["status"] == "completed" and r["conclusion"] == "success"
    for r in runs
)
jobs = [j for r in runs for j in r["jobs"]]
assert {j["name"] for j in jobs} == expected and len(jobs) == 15
assert all(j["conclusion"] == "success" for j in jobs)
summary = json.loads(
    subprocess.check_output(["python3", str(Path(__file__).with_name("summarize_reports.py")), str(root)])
)
for report in summary:
    if report["type"] == "junit":
        assert not report["counts"].get("failed") and not report["counts"].get("errors"), report
    else:
        assert report["combined_percent"] >= 80, report
assert len([r for r in summary if r["type"] == "coverage"]) == 5
for version in ("3.10", "3.11", "3.12", "3.13", "3.14"):
    for report in ("unit.xml", "integration.xml", "coverage.xml"):
        assert any(
            f"test-reports-python-{version}/" in r["file"] and r["file"].endswith("/" + report)
            for r in summary
        )
runtime = []
for p in sorted(root.rglob("runtime.txt")):
    assert p.read_text().splitlines()[0] == checkout, (str(p), p.read_text().splitlines()[0])
    runtime.append(str(p.relative_to(root)))
assert len(runtime) == 7
recovery = list(root.rglob("backend-recovery.json"))
assert len(recovery) == 1
rec = json.loads(recovery[0].read_text())
assert rec["status"] == "response_received" and rec["elapsed_seconds"] < rec["deadline_seconds"]
result = {
    "source_revision": source,
    "checkout_revision": checkout,
    "runs": [
        {k: r[k] for k in ("url", "event", "attempt", "headSha", "status", "conclusion")}
        for r in runs
    ],
    "required_checks": 15,
    "all_checks_passed": True,
    "runtime_identity_files": runtime,
    "backend_restart_recovery": rec,
    "reports": summary,
    "scope": "Hosted Ubuntu matrix and native libmagic; isolated PostgreSQL/NATS/MinIO/Azurite; GCS emulator; disposable kind; mocked Terraform. Not frozen-candidate, live-cloud, production, full-scale movement, soak or pilot qualification.",
    "overlap": "Suite counts overlap and must not be summed.",
}
(root / "qualification-review.json").write_text(json.dumps(result, indent=2) + "\n")
print(
    json.dumps(
        {k: v for k, v in result.items() if k not in ("reports", "runtime_identity_files")},
        indent=2,
    )
)
