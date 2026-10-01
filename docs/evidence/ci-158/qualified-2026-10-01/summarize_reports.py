"""Summarize retained JUnit/coverage without adding overlapping suites."""

import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
summaries = []
for path in sorted(root.rglob("*.xml")):
    if path.name == "coverage.xml":
        data = ET.parse(path).getroot()
        lines = int(data.get("lines-valid", 0))
        branches = int(data.get("branches-valid", 0))
        covered = int(data.get("lines-covered", 0)) + int(data.get("branches-covered", 0))
        summaries.append(
            {
                "file": str(path.relative_to(root)),
                "type": "coverage",
                "line_percent": 100 * float(data.get("line-rate", 0)),
                "branch_percent": 100 * float(data.get("branch-rate", 0)),
                "combined_percent": 100 * covered / (lines + branches)
                if lines + branches
                else None,
            }
        )
        continue
    try:
        data = ET.parse(path).getroot()
    except ET.ParseError:
        continue
    cases = data.findall(".//testcase")
    if not cases:
        continue
    counts = Counter()
    reasons = Counter()
    for case in cases:
        skipped = case.find("skipped")
        if skipped is not None:
            kind = "xfail" if skipped.get("type") == "pytest.xfail" else "skipped"
            counts[kind] += 1
            reasons[skipped.get("message", "unspecified")] += 1
        elif case.find("failure") is not None:
            counts["failed"] += 1
        elif case.find("error") is not None:
            counts["errors"] += 1
        else:
            counts["passed"] += 1
    summaries.append(
        {
            "file": str(path.relative_to(root)),
            "type": "junit",
            "counts": dict(counts),
            "skip_reasons": dict(reasons),
        }
    )
print(json.dumps(summaries, indent=2))
