"""Opt-in MinIO execution of the shared storage-driver conformance suite.

The tests are deliberately disabled unless all required MinIO settings are
present.  They never fall back to the ambient AWS credential chain.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.client import Config
from botocore.exceptions import ClientError

from cognistore.core.catalog import Catalog
from cognistore.core.consistency import ConsistencyScanner, ScanScope
from cognistore.core.mover import (
    MoveGenerationMismatchError,
    Mover,
    MoveVerificationError,
)
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.s3_driver import S3Driver
from tests.conformance.storage_driver import StorageDriverConformance

_REQUIRED_ENV = (
    "COGNISTORE_MINIO_ENDPOINT_URL",
    "COGNISTORE_MINIO_ACCESS_KEY",
    "COGNISTORE_MINIO_SECRET_KEY",
)
_missing_env = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
if _missing_env:
    pytest.skip(
        "MinIO conformance requires " + ", ".join(_missing_env),
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


def _is_not_found(error: ClientError) -> bool:
    response = error.response
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = str(response.get("Error", {}).get("Code", ""))
    return status == 404 or code in {"404", "NoSuchBucket", "NotFound"}


@pytest.fixture(scope="module")
def minio_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["COGNISTORE_MINIO_ENDPOINT_URL"],
        region_name=os.environ.get("COGNISTORE_MINIO_REGION", "us-east-1"),
        aws_access_key_id=os.environ["COGNISTORE_MINIO_ACCESS_KEY"],
        aws_secret_access_key=os.environ["COGNISTORE_MINIO_SECRET_KEY"],
        aws_session_token=os.environ.get("COGNISTORE_MINIO_SESSION_TOKEN"),
        config=Config(s3={"addressing_style": "path"}),
    )


def _cleanup_bucket(client: Any, bucket: str) -> None:
    """Remove completed objects and unfinished uploads from a test bucket."""

    try:
        uploads = client.get_paginator("list_multipart_uploads")
        for page in uploads.paginate(Bucket=bucket):
            for upload in page.get("Uploads", []):
                try:
                    client.abort_multipart_upload(
                        Bucket=bucket,
                        Key=upload["Key"],
                        UploadId=upload["UploadId"],
                    )
                except ClientError as error:
                    code = str(error.response.get("Error", {}).get("Code", ""))
                    if code == "NoSuchUpload":
                        continue
                    if code in {"NoSuchBucket", "NotFound"}:
                        return
                    raise
    except ClientError as error:
        if _is_not_found(error):
            return
        raise

    try:
        objects = client.get_paginator("list_objects_v2")
        for page in objects.paginate(Bucket=bucket):
            for item in page.get("Contents", []):
                client.delete_object(Bucket=bucket, Key=item["Key"])
        client.delete_bucket(Bucket=bucket)
    except ClientError as error:
        if not _is_not_found(error):
            raise


class TestMinioS3DriverConformance(StorageDriverConformance):
    @pytest.fixture
    def driver(self, minio_client) -> S3Driver:
        # Five objects are listed by the shared suite.  A page size of two
        # ensures that both unfiltered and alpha-prefixed listings paginate.
        return S3Driver(
            endpoint_url=os.environ["COGNISTORE_MINIO_ENDPOINT_URL"],
            region_name=os.environ.get("COGNISTORE_MINIO_REGION", "us-east-1"),
            access_key=os.environ["COGNISTORE_MINIO_ACCESS_KEY"],
            secret_key=os.environ["COGNISTORE_MINIO_SECRET_KEY"],
            session_token=os.environ.get("COGNISTORE_MINIO_SESSION_TOKEN"),
            addressing_style="path",
            auto_create_bucket=True,
            list_page_size=2,
            client=minio_client,
        )

    @pytest.fixture
    def bucket(self, minio_client) -> Iterator[str]:
        bucket = f"cognistore-conformance-{uuid.uuid4().hex}"
        yield bucket

        # Each conformance case owns its bucket.  Clean up directly through the
        # client so a failed assertion does not leave MinIO state behind.
        _cleanup_bucket(minio_client, bucket)


@pytest.fixture
def move_bucket(minio_client) -> Iterator[str]:
    bucket = f"cognistore-move-{uuid.uuid4().hex}"
    yield bucket
    _cleanup_bucket(minio_client, bucket)


def _multipart_driver(client: Any) -> S3Driver:
    part_size = 5 * 1024 * 1024
    return S3Driver(
        endpoint_url=os.environ["COGNISTORE_MINIO_ENDPOINT_URL"],
        region_name=os.environ.get("COGNISTORE_MINIO_REGION", "us-east-1"),
        addressing_style="path",
        auto_create_bucket=True,
        chunk_size=part_size,
        multipart_threshold=part_size,
        client=client,
    )


def test_s3_inventory_pages_and_resumed_consistency_scan_are_scoped_and_read_only(
    tmp_path: Path,
    minio_client,
    move_bucket: str,
) -> None:
    prefix = "tenants/雪%_/"
    tracked = [prefix + name for name in ("alpha", "beta", "nested/gamma", "omega")]
    untracked = prefix + "untracked"
    outside = "tenants/other/private"
    all_keys = tracked + [untracked, outside]
    payload = b"complete checksum content"
    driver = _multipart_driver(minio_client)
    for key in all_keys:
        driver.put_object(move_bucket, key, payload)

    cursor = None
    listed: list[str] = []
    page_lengths = []
    while True:
        # Tokens must survive client/driver reconstruction, as CLI resumes do.
        page = _multipart_driver(minio_client).list_objects_page(
            move_bucket, prefix, cursor=cursor, limit=2,
        )
        page_lengths.append(len(page.keys))
        listed.extend(page.keys)
        if page.next_cursor is None:
            break
        assert page.next_cursor != cursor
        cursor = page.next_cursor
    assert listed == sorted(tracked + [untracked])
    assert page_lengths == [2, 2, 1]

    database = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(database) as catalog:
        for key in tracked + [outside]:
            catalog.upsert(
                move_bucket, key, size=len(payload), tier="hot",
                metadata={"sha256": hashlib.sha256(payload).hexdigest(), "sample_len": len(payload)},
            )
    catalog_before = database.read_bytes()
    objects_before = {key: driver.stat_object(move_bucket, key) for key in all_keys}
    scope = ScanScope("tenant-unicode", move_bucket, prefix, ("hot",))
    report = tmp_path / "consistency.sqlite3"
    reports = []
    with SQLiteCatalog(database, read_only=True) as catalog:
        for attempt in range(30):
            scanner = ConsistencyScanner(
                catalog, {"hot": _multipart_driver(minio_client)}, scope,
                binding_id="minio-isolated-integration", page_size=2,
                requests_per_second=1000, bytes_per_second=1e9,
            )
            summary = scanner.run(report, resume=attempt > 0, max_items=2)
            reports.append(summary)
            if summary["complete"]:
                break
    assert len(reports) > 1 and not reports[0]["complete"]
    assert summary["complete"] and summary["read_only"]
    assert summary["checked"] == summary["inventory_keys"] == 5
    assert summary["reason_counts"] == {"untracked_object": 1}
    with sqlite3.connect(report) as connection:
        inventory = {row[0] for row in connection.execute("SELECT object_key FROM inventory")}
        findings = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM findings")]
    assert inventory == set(tracked + [untracked])
    assert [finding["key"] for finding in findings] == [untracked]
    assert all(finding["tenant_id"] == scope.tenant_id for finding in findings)
    assert database.read_bytes() == catalog_before
    assert {key: driver.stat_object(move_bucket, key) for key in all_keys} == objects_before
    assert set(driver.list_objects(move_bucket)) == set(all_keys)
    assert all(driver.get_object(move_bucket, key) == payload for key in all_keys)


def test_mover_streams_posix_to_s3_and_back(
    tmp_path: Path,
    minio_client,
    move_bucket: str,
) -> None:
    part_size = 5 * 1024 * 1024
    payload = b"a" * (2 * part_size) + b"non-aligned-tail"
    bucket = move_bucket
    key = "nested/large.bin"
    hot = PosixDriver(str(tmp_path / "hot"), chunk_size=64 * 1024)
    warm = PosixDriver(str(tmp_path / "warm"), chunk_size=64 * 1024)
    object_store = _multipart_driver(minio_client)
    catalog = Catalog()

    hot.put_object(bucket, key, payload)
    catalog.upsert(bucket, key, len(payload), "hot", metadata={"mime": "test/data"})
    Mover({"hot": hot, "object": object_store}, catalog).move(
        "hot", "object", bucket, key
    )

    assert not (hot.base / bucket / key).exists()
    assert object_store.stat_object(bucket, key)["size"] == len(payload)
    assert catalog.get(bucket, key).tier == "object"  # type: ignore[union-attr]

    Mover({"object": object_store, "warm": warm}, catalog).move(
        "object", "warm", bucket, key
    )

    assert warm.get_object(bucket, key) == payload
    with pytest.raises(FileNotFoundError):
        object_store.stat_object(bucket, key)
    record = catalog.get(bucket, key)
    assert record is not None
    assert (record.tier, record.size, record.metadata) == (
        "warm",
        len(payload),
        {"mime": "test/data", "sha256": hashlib.sha256(payload).hexdigest()},
    )


@pytest.mark.parametrize("damage", ["truncate", "corrupt"])
def test_mover_rejects_damaged_s3_destination_and_preserves_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    minio_client,
    move_bucket: str,
    damage: str,
) -> None:
    bucket = move_bucket
    key = f"damaged/{damage}.bin"
    payload = b"verify the committed S3 bytes before deleting POSIX source"
    source = PosixDriver(str(tmp_path / f"{damage}-source"))
    destination = _multipart_driver(minio_client)
    catalog = Catalog()
    source.put_object(bucket, key, payload)
    catalog.upsert(
        bucket,
        key,
        len(payload),
        "source",
        metadata={"sha256": "unverified-scan-value"},
    )
    original_put_stream = destination.put_object_stream

    def damage_committed_destination(*args: Any, **kwargs: Any) -> int:
        written = original_put_stream(*args, **kwargs)
        damaged = payload[:-1] if damage == "truncate" else b"X" + payload[1:]
        minio_client.put_object(Bucket=bucket, Key=key, Body=damaged)
        return written

    monkeypatch.setattr(
        destination,
        "put_object_stream",
        damage_committed_destination,
    )

    with pytest.raises(MoveVerificationError) as captured:
        Mover({"source": source, "destination": destination}, catalog).move(
            "source", "destination", bucket, key
        )

    result = captured.value.result
    assert result.source_checksum == hashlib.sha256(payload).hexdigest()
    assert result.destination_checksum is not None
    assert result.destination_checksum != result.source_checksum
    assert result.source_checksum in str(captured.value)
    assert result.destination_checksum in str(captured.value)
    assert source.get_object(bucket, key) == payload
    record = catalog.get(bucket, key)
    assert record is not None
    assert (record.tier, record.size, record.metadata) == (
        "source",
        len(payload),
        {"sha256": "unverified-scan-value"},
    )


def test_s3_source_replacement_is_not_deleted_during_cleanup(
    tmp_path: Path,
    minio_client,
    move_bucket: str,
) -> None:
    bucket = move_bucket
    key = "races/source-replacement.bin"
    original = b"old-version"
    replacement = b"new-version"
    source = _multipart_driver(minio_client)
    destination = PosixDriver(str(tmp_path / "destination"))
    catalog = Catalog()
    source.put_object(bucket, key, original)
    catalog.upsert(bucket, key, len(original), "source")

    def replace_source_at_cleanup(job) -> None:
        if job.state.value == "cleanup":
            minio_client.put_object(Bucket=bucket, Key=key, Body=replacement)

    mover = Mover(
        {"source": source, "destination": destination},
        catalog,
        transition_hook=replace_source_at_cleanup,
    )

    with pytest.raises(MoveGenerationMismatchError, match="source generation"):
        mover.move("source", "destination", bucket, key)

    assert source.get_object(bucket, key) == replacement
    assert destination.get_object(bucket, key) == original
    record = catalog.get(bucket, key)
    assert record is not None
    assert (record.tier, record.size) == ("source", len(replacement))


def test_s3_destination_replacement_after_final_verification_retains_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    minio_client,
    move_bucket: str,
) -> None:
    bucket = move_bucket
    key = "races/destination-replacement.bin"
    original = b"good-version"
    replacement = b"evil-version"
    source = PosixDriver(str(tmp_path / "source"))
    destination = _multipart_driver(minio_client)
    catalog = Catalog()
    source.put_object(bucket, key, original)
    catalog.upsert(bucket, key, len(original), "source")
    mover = Mover({"source": source, "destination": destination}, catalog)
    original_verify = mover._verify_committed_destination

    def replace_after_verification(*args: Any, **kwargs: Any) -> None:
        original_verify(*args, **kwargs)
        minio_client.put_object(Bucket=bucket, Key=key, Body=replacement)

    monkeypatch.setattr(mover, "_verify_committed_destination", replace_after_verification)

    with pytest.raises(MoveGenerationMismatchError, match="destination generation"):
        mover.move("source", "destination", bucket, key)

    assert source.get_object(bucket, key) == original
    assert destination.get_object(bucket, key) == replacement
    record = catalog.get(bucket, key)
    assert record is not None
    assert (record.tier, record.size) == ("source", len(original))


def test_interrupted_minio_multipart_is_aborted(
    tmp_path: Path,
    minio_client,
    move_bucket: str,
) -> None:
    class InterruptingClient:
        def __init__(self, wrapped: Any) -> None:
            self._wrapped = wrapped
            self.meta = wrapped.meta
            self.uploads = 0

        def __getattr__(self, name: str) -> Any:
            return getattr(self._wrapped, name)

        def upload_part(self, **kwargs: Any) -> dict[str, Any]:
            self.uploads += 1
            if self.uploads == 2:
                raise InterruptedError("simulated transfer interruption")
            return self._wrapped.upload_part(**kwargs)

    part_size = 5 * 1024 * 1024
    bucket = move_bucket
    key = "interrupted.bin"
    source = PosixDriver(str(tmp_path / "source"), chunk_size=64 * 1024)
    destination = _multipart_driver(InterruptingClient(minio_client))
    catalog = Catalog()
    source.put_object(bucket, key, b"x" * (part_size + 1))
    catalog.upsert(bucket, key, part_size + 1, "source")

    with pytest.raises(InterruptedError, match="simulated transfer"):
        Mover({"source": source, "destination": destination}, catalog).move(
            "source", "destination", bucket, key
        )

    assert source.stat_object(bucket, key)["size"] == part_size + 1
    record = catalog.get(bucket, key)
    assert record is not None
    assert record.tier == "source"
    with pytest.raises(ClientError) as missing:
        minio_client.head_object(Bucket=bucket, Key=key)
    assert _is_not_found(missing.value)

    response = minio_client.list_multipart_uploads(Bucket=bucket, Prefix=key)
    assert response.get("Uploads", []) == []
