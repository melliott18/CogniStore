#!/usr/bin/env python3
"""Build deterministic synthetic m5-pilot-v1 corpus manifests, entirely offline.

Every object is generated and hashed, but object files are written only with
--write-payloads. Memory use is bounded by a few copies of one 16 MiB object,
independent of corpus size. This generates inputs, never qualification results.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

GENERATOR_VERSION = "m5-corpus-v1"
SPECIFICATION = "m5-pilot-v1"
DEFAULT_SEED = "m5-pilot-v1"
TENANTS = ("pilot-a", "pilot-b")
SIZE_PERCENTAGES = ((4096, 60), (65536, 30), (1048576, 9), (16777216, 1))
OPAQUE = "application/octet-stream"
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME_PERCENTAGES = ((OPAQUE, 80), (PDF, 10), (DOCX, 10))


@dataclass(frozen=True)
class ObjectSpec:
    tenant: str
    key: str
    size_bytes: int
    content_type: str


def validate_count(object_count: int) -> None:
    if object_count < 1000 or object_count % 1000:
        raise ValueError("object count must be a positive multiple of 1,000")


def iter_object_specs(object_count: int = 100000) -> Iterator[ObjectSpec]:
    """Yield exact global size/MIME cells and equal tenant counts and bytes.

    Both tenants have identical keys and sizes. At multiples of 2,000, every
    tenant also has the exact joint histogram. At odd multiples of 1,000, the
    odd PDF/DOCX cells are shared across tenants; each tenant still has exactly
    80/10/10 percent MIME counts and the selected size distribution.
    """
    validate_count(object_count)
    key_offset = 0
    for size, size_percent in SIZE_PERCENTAGES:
        size_count = object_count * size_percent // 100
        ordinal = 0
        # Alternating the start at the last bin balances odd document cells.
        first_tenant = key_offset % 2
        for content_type, mime_percent in MIME_PERCENTAGES:
            for _ in range(size_count * mime_percent // 100):
                yield ObjectSpec(
                    tenant=TENANTS[(ordinal + first_tenant) % 2],
                    key=f"corpus/{key_offset + ordinal // 2:08d}",
                    size_bytes=size,
                    content_type=content_type,
                )
                ordinal += 1
        key_offset += size_count // 2


def payload_text(spec: ObjectSpec, seed: str) -> str:
    """Short searchable text; even the largest document has tiny extraction."""
    identity = json.dumps(
        {"generator": GENERATOR_VERSION, "seed": seed, **asdict(spec)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")
    return f"CogniStore synthetic {spec.tenant} {spec.key} {hashlib.sha256(identity).hexdigest()}"


def _pdf(text: str, target_size: int) -> bytes:
    # A complete one-page PDF with a real text stream, accurate xref offsets,
    # and legal whitespace before xref (not garbage appended after EOF).
    stream = f"BT /F1 9 Tf 30 750 Td ({text}) Tj ET\n".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream",
    ]
    document = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref = b"xref\n0 6\n0000000000 65535 f \n" + b"".join(
        f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets[1:]
    )
    xref += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
    # xref's location contributes decimal digits to the final file length.
    xref_offset = target_size - len(xref) - len(str(target_size)) - len(b"\n%%EOF\n")
    while True:
        revised = target_size - len(xref) - len(str(xref_offset)) - len(b"\n%%EOF\n")
        if revised == xref_offset:
            break
        xref_offset = revised
    if xref_offset < len(document):
        raise ValueError("target size cannot contain the PDF structure")
    document.extend(b"\n" * (xref_offset - len(document)))
    document.extend(xref + str(xref_offset).encode("ascii") + b"\n%%EOF\n")
    return bytes(document)


def _docx(text: str, target_size: int) -> bytes:
    # Minimal OOXML Word package. Padding is an XML comment inside the document,
    # so the ZIP and its XML remain valid and normalized text stays small.
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/></Types>'
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>'
        ),
        "word/document.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p><w:sectPr/></w:body>'
            '<!--PADDING--></w:document>'
        ),
    }
    # ZIP_STORED has deterministic, linear overhead (no data descriptors).
    base_size = 22 + sum(
        30 + 46 + 2 * len(name.encode("ascii")) + len(data.encode("ascii"))
        for name, data in parts.items()
    )
    padding_size = target_size - base_size
    if padding_size < 0:
        raise ValueError("target size cannot contain the DOCX structure")
    # Separate small comments avoid XML parser limits on single large nodes.
    comment = "<!--" + " " * 1000 + "-->"
    padding = comment * (padding_size // len(comment)) + " " * (padding_size % len(comment))
    parts["word/document.xml"] = parts["word/document.xml"].replace(
        "<!--PADDING-->", "<!--PADDING-->" + padding
    )
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in parts.items():
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 0
            archive.writestr(entry, data.encode("ascii"))
    return result.getvalue()


def generate_payload(spec: ObjectSpec, seed: str = DEFAULT_SEED) -> bytes:
    """Generate one deterministic object; never reads credentials or networks."""
    if spec.tenant not in TENANTS or spec.size_bytes not in dict(SIZE_PERCENTAGES):
        raise ValueError("unsupported tenant or size")
    # Keys are interpolated into both PDF and XML, so restrict them to the
    # generator's safe logical-key alphabet, including callers of this API.
    if not spec.key or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789/-_"
                           for character in spec.key):
        raise ValueError("key must use lowercase letters, digits, slash, hyphen or underscore")
    text = payload_text(spec, seed)
    if spec.content_type == PDF:
        payload = _pdf(text, spec.size_bytes)
    elif spec.content_type == DOCX:
        payload = _docx(text, spec.size_bytes)
    elif spec.content_type == OPAQUE:
        # High-entropy bytes avoid making the opaque storage cohort artificially
        # compressible. The marker ensures these are not accidentally text files.
        marker = b"\x00CogniStore synthetic opaque\x00"
        payload = marker + hashlib.shake_256(text.encode("ascii")).digest(
            spec.size_bytes - len(marker)
        )
    else:
        raise ValueError("unsupported content type")
    if len(payload) != spec.size_bytes:
        raise AssertionError("generator did not produce the declared object size")
    return payload


def build_corpus(
    output: Path, *, object_count: int = 100000, seed: str = DEFAULT_SEED,
    write_payloads: bool = False,
) -> dict:
    """Create a new evidence directory, refusing existing paths and partial runs.

    summary.json is written last. Its absence marks an incomplete generation.
    The manifest is streamed and no whole-corpus list is retained.
    """
    validate_count(object_count)
    if not seed or len(seed.encode("utf-8")) > 1024:
        raise ValueError("seed must contain 1 to 1,024 UTF-8 bytes")
    output.mkdir(parents=True, exist_ok=False)
    size_counts: Counter[str] = Counter()
    mime_counts: Counter[str] = Counter()
    joint_counts: Counter[tuple[str, str]] = Counter()
    tenant_joint_counts: Counter[tuple[str, str, str]] = Counter()
    tenant_counts: Counter[str] = Counter()
    tenant_bytes: Counter[str] = Counter()
    manifest_digest = hashlib.sha256()
    with (output / "manifest.jsonl").open("xb") as manifest:
        for spec in iter_object_specs(object_count):
            payload = generate_payload(spec, seed)
            relative_path = f"payloads/{spec.tenant}/{spec.key}" if write_payloads else None
            row = {**asdict(spec), "sha256": hashlib.sha256(payload).hexdigest(),
                   "payload_path": relative_path}
            if relative_path:
                path = output / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as object_file:
                    object_file.write(payload)
            line = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
            manifest.write(line)
            manifest_digest.update(line)
            size_counts[str(spec.size_bytes)] += 1
            mime_counts[spec.content_type] += 1
            joint_counts[(str(spec.size_bytes), spec.content_type)] += 1
            tenant_joint_counts[(spec.tenant, str(spec.size_bytes), spec.content_type)] += 1
            tenant_counts[spec.tenant] += 1
            tenant_bytes[spec.tenant] += spec.size_bytes
            del payload
    summary = {
        "generator_version": GENERATOR_VERSION,
        "generator_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "specification": SPECIFICATION,
        "seed": seed,
        "seed_sha256": hashlib.sha256(seed.encode("utf-8")).hexdigest(),
        "object_count": object_count,
        "logical_payload_bytes": sum(tenant_bytes.values()),
        "size_histogram": dict(size_counts),
        "mime_histogram": dict(mime_counts),
        "joint_histogram": {
            size: {mime: joint_counts[(size, mime)] for mime, _ in MIME_PERCENTAGES}
            for size in size_counts
        },
        "tenant_object_counts": dict(tenant_counts),
        "tenant_joint_histograms": {
            tenant: {size: {mime: tenant_joint_counts[(tenant, size, mime)]
                            for mime, _ in MIME_PERCENTAGES} for size in size_counts}
            for tenant in TENANTS
        },
        "tenant_logical_bytes": dict(tenant_bytes),
        "manifest_sha256": manifest_digest.hexdigest(),
        "payloads_written": write_payloads,
        "qualification_status": "input-generation-only; no deployment qualification performed",
        "document_padding": "valid PDF whitespace and DOCX XML comments; tiny extracted text",
    }
    with (output / "summary.json").open("x", encoding="utf-8") as summary_file:
        json.dump(summary, summary_file, indent=2, sort_keys=True)
        summary_file.write("\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path,
                        help="fresh output directory; existing paths are never overwritten")
    parser.add_argument("--objects", type=int, default=100000,
                        help="multiple of 1,000; 1,000 local, 100,000 nominal, 200,000 capacity")
    parser.add_argument("--seed", default=DEFAULT_SEED, help="public reproducibility seed")
    parser.add_argument("--write-payloads", action="store_true",
                        help="also materialize payloads (26.474 GiB per 100,000 objects)")
    args = parser.parse_args(argv)
    try:
        summary = build_corpus(args.output, object_count=args.objects, seed=args.seed,
                               write_payloads=args.write_payloads)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"load-corpus: {exc}\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
