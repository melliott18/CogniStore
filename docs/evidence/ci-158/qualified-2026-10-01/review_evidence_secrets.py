"""Require each raw-report scanner finding to equal a verified source digest."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
source = sys.argv[2]
report = root.parent / (root.name + "-secrets.json")
log = root.parent / (root.name + "-secrets.log")
with log.open("w") as out:
    result = subprocess.run(
        [
            "gitleaks",
            "dir",
            str(root),
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
for f in json.loads(report.read_text()):
    p = Path(f["File"])
    assert p.name == "operator-drill.source-sha256.json" and f["RuleID"] == "generic-api-key", (
        str(p),
        f["StartLine"],
    )
    key, value = next(
        iter(
            json.loads(
                "{" + p.read_text().splitlines()[f["StartLine"] - 1].strip().rstrip(",") + "}"
            ).items()
        )
    )
    assert (
        hashlib.sha256(subprocess.check_output(["git", "show", source + ":" + key])).hexdigest()
        == value
    )
    review.append(
        {
            "file": str(p.relative_to(root)),
            "line": f["StartLine"],
            "classification": "SHA-256 digest verified against source Git object",
            "source_path": key,
        }
    )
(root / "evidence-secret-review.json").write_text(json.dumps(review, indent=2) + "\n")
print("Reviewed", len(review), "source digest findings")
