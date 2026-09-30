#!/usr/bin/env python3
"""Import already observed private deployment evidence without inventing fields.

The staging collector writes the supplemental FILES contract into a private
spool. This hook validates every record's candidate/run binding, copies the
exact bytes, and retains their hashes. Semantic acceptance remains a separate
step. Missing observations fail import; they are never replaced with zeros.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

try:
    from scripts import load_acceptance as acceptance
    from scripts import load_qualification as base
except ModuleNotFoundError:
    import load_acceptance as acceptance
    import load_qualification as base

FILES = tuple(name for name in acceptance.FILES
              if name not in ("campaign.json", "foreground.jsonl", "telemetry.jsonl"))
MAX_ROW = 4 * 1024 * 1024
MAX_JSONL_ROW = 65536


def import_evidence(source: Path, output: Path, run_id: str, binding_sha256: str):
    source, output = source.resolve(), output.resolve()
    campaign = acceptance.read_json(acceptance.fixed_file(output, "campaign.json"))
    if (campaign.get("run_id") != run_id
            or acceptance.binding_digest(campaign) != binding_sha256 or source == output):
        raise base.InvalidEvidence("export_identity_mismatch")
    names = (*FILES, "export-manifest.json")
    if any((output / name).exists() or (output / name).is_symlink() for name in names):
        raise base.InvalidEvidence("export_output_exists")
    hashes = {}
    # Validate the snapshot, not a second read of potentially changing inputs.
    with tempfile.TemporaryDirectory(prefix=".load-export-", dir=output) as scratch:
        staging = Path(scratch)
        for name in FILES:
            path = source / name
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                raise base.InvalidEvidence("missing_or_unsafe_export") from None
            with os.fdopen(descriptor, "rb") as incoming, (staging / name).open("xb") as target:
                if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                    raise base.InvalidEvidence("missing_or_unsafe_export")
                checksum = hashlib.sha256()
                if name.endswith(".json"):
                    raw = incoming.read(MAX_ROW + 1)
                    rows = [raw]
                else:
                    rows = iter(lambda: incoming.readline(MAX_JSONL_ROW + 1), b"")
                count = 0
                for raw in rows:
                    if len(raw) > (MAX_ROW if name.endswith(".json") else MAX_JSONL_ROW):
                        raise base.InvalidEvidence("export_record_too_large")
                    row = base.decode(raw)
                    if (not isinstance(row, dict) or row.get("run_id") != run_id
                            or row.get("binding_sha256") != binding_sha256):
                        raise base.InvalidEvidence("export_record_identity_mismatch")
                    if name == "observed-artifacts.jsonl":
                        content = row.get("content", {})
                        if (not isinstance(content, dict) or content.get("run_id") != run_id
                                or content.get("binding_sha256") != binding_sha256):
                            raise base.InvalidEvidence("export_source_identity_mismatch")
                    checksum.update(raw)
                    target.write(raw)
                    count += 1
                if count == 0:
                    raise base.InvalidEvidence("empty_export")
                target.flush()
                os.fsync(target.fileno())
                hashes[name] = checksum.hexdigest()
        manifest = {"schema_version": 1, "run_id": run_id, "binding_sha256": binding_sha256,
                    "artifacts": hashes, "status": "imported-unreviewed"}
        (staging / "export-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        published = []
        try:
            for name in names:
                # Exclusive creation prevents overwriting runner or earlier evidence.
                with (output / name).open("xb") as target:
                    published.append(output / name)
                    with (staging / name).open("rb") as incoming:
                        shutil.copyfileobj(incoming, target)
                    target.flush()
                    os.fsync(target.fileno())
        except BaseException:
            for path in published:
                path.unlink()
            raise
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--binding-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        import_evidence(args.source, args.output, args.run_id, args.binding_sha256)
    except (OSError, ValueError, KeyError, TypeError):
        print("load export: failed (missing, unsafe or mismatched evidence)")
        return 1
    print("load export: imported; acceptance review required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
