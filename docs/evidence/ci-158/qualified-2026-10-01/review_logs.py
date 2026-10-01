"""Review decompressed hosted logs before publishing the evidence archive."""

import gzip
import hashlib
import json
import subprocess
from pathlib import Path

root = Path("test-results/158")
expanded = root / "expanded-log-review"
for name in (
    "pr-first-evidence",
    "pr-second-evidence",
    "pr-third-evidence",
    "pr-final-evidence",
    "main-evidence",
):
    for p in (root / name).rglob("*.log.gz"):
        dest = expanded / name / p.relative_to(root / name).with_suffix("")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(gzip.decompress(p.read_bytes()))
report = root / "expanded-log-secrets.json"
with (root / "expanded-log-secrets.log").open("w") as out:
    result = subprocess.run(
        [
            "gitleaks",
            "dir",
            str(expanded),
            "--redact",
            "--report-format",
            "json",
            "--report-path",
            str(report),
        ],
        stdout=out,
        stderr=subprocess.STDOUT,
    )
assert result.returncode in (0, 1), result.returncode
review = []
workflow = Path(".github/workflows/ci.yml").read_text()
fixture = next(
    line.split(": ", 1)[1].strip().strip('"')
    for line in workflow.splitlines()
    if line.strip().startswith("COGNISTORE_AZURITE_CONNECTION_STRING:")
)
for f in json.loads(report.read_text()):
    p = Path(f["File"])
    line = p.read_text().splitlines()[f["StartLine"] - 1]
    assert f["RuleID"] == "generic-api-key" and line.endswith(
        "COGNISTORE_AZURITE_CONNECTION_STRING: " + fixture
    ), (str(p), f["StartLine"])
    review.append(
        {
            "file": str(p.relative_to(expanded)),
            "line": f["StartLine"],
            "classification": "Known loopback-only Azurite development fixture credential, exactly equal to workflow configuration",
            "line_sha256": hashlib.sha256(line.encode()).hexdigest(),
        }
    )
(root / "expanded-log-review.json").write_text(json.dumps(review, indent=2) + "\n")
print("Verified", len(review), "exact loopback fixture occurrences in decompressed job logs")
