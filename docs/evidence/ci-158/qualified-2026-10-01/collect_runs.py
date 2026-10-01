"""Archive this authorized campaign's completed runs; never dispatch or rerun."""

import gzip
import json
import subprocess
import sys
from pathlib import Path


def gh(*args):
    return subprocess.check_output(["gh", *args])


base = Path(sys.argv[1])
base.mkdir(parents=True, exist_ok=True)
for run_id in sys.argv[2:]:
    out = base / run_id
    out.mkdir(exist_ok=True)
    run = json.loads(
        gh(
            "run",
            "view",
            run_id,
            "--repo",
            "melliott18/CogniStore",
            "--json",
            "url,headSha,event,attempt,status,conclusion,jobs",
        )
    )
    assert run["status"] == "completed", run_id
    (out / "run.json").write_text(json.dumps(run, indent=2) + "\n")
    artifacts = gh("api", f"repos/melliott18/CogniStore/actions/runs/{run_id}/artifacts")
    (out / "artifacts.json").write_bytes(artifacts)
    reports = out / "reports"
    if not reports.exists():
        subprocess.run(
            [
                "gh",
                "run",
                "download",
                run_id,
                "--repo",
                "melliott18/CogniStore",
                "--dir",
                str(reports),
            ],
            check=True,
        )
    collection = []
    for job in run["jobs"]:
        target = out / f"job-{job['databaseId']}.log.gz"
        try:
            if target.exists():
                data = gzip.decompress(target.read_bytes())
            else:
                data = gh("api", f"repos/melliott18/CogniStore/actions/jobs/{job['databaseId']}/logs")
                target.write_bytes(gzip.compress(data, mtime=0))
            collection.append(
                {
                    "job": job["databaseId"],
                    "bytes": len(data),
                    "ends_with_cleanup": b"Cleaning up orphan processes" in data[-1000:],
                }
            )
        except subprocess.CalledProcessError as exc:
            collection.append({"job": job["databaseId"], "error": str(exc)})
    (out / "log-collection.json").write_text(json.dumps(collection, indent=2) + "\n")
    print(run_id, run["headSha"], run["conclusion"], len(run["jobs"]), flush=True)
