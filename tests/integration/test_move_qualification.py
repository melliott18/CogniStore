from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from cognistore.drivers.driver_loader import load_drivers
from cognistore.drivers.s3_driver import S3Driver
from tests.perf.qualification import (
    PathConfig,
    QualificationConfig,
    SizeClass,
    run_qualification,
)

MINIO_ENDPOINT = os.environ.get("COGNISTORE_MINIO_ENDPOINT_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not MINIO_ENDPOINT,
        reason="set COGNISTORE_MINIO_ENDPOINT_URL to an isolated MinIO service",
    ),
]


def test_reduced_s3_round_trip_and_fault_recovery(tmp_path: Path) -> None:
    token = uuid4().hex[:12]
    bucket = f"qualification-{token}"
    drivers_path = tmp_path / "drivers.yaml"
    drivers_path.write_text(
        """
tiers:
  hot:
    driver: posix
    path: {hot}
    chunk_size: 64
  object:
    driver: s3
    endpoint_url: {endpoint}
    region_name: {region}
    access_key_env: COGNISTORE_MINIO_ACCESS_KEY
    secret_key_env: COGNISTORE_MINIO_SECRET_KEY
    addressing_style: path
    auto_create_bucket: true
    chunk_size: 5242880
    multipart_threshold: 8388608
""".format(
            hot=tmp_path / "hot",
            endpoint=MINIO_ENDPOINT,
            region=os.environ.get("COGNISTORE_MINIO_REGION", "us-east-1"),
        ),
        encoding="utf-8",
    )
    config = QualificationConfig(
        drivers_path=drivers_path,
        catalog_path=tmp_path / "qualification.sqlite3",
        output_path=tmp_path / "qualification.json",
        run_id=f"integration-{token}",
        bucket=bucket,
        object_count=5,
        size_classes=(SizeClass(0), SizeClass(257), SizeClass(4097)),
        paths=(PathConfig("s3", "hot", "object"),),
        workers=2,
        retry_base_delay=0.05,
        retry_max_delay=0.05,
        lease_seconds=0.02,
        worker_termination_timeout=10.0,
    )

    try:
        report = run_qualification(config)
        assert report["status"] == "passed"
        assert report["paths"][0]["backend_path"] == "PosixDriver->S3Driver"
        assert report["paths"][0]["integrity"]["forward"]["verified_objects"] == 5
        assert report["paths"][0]["integrity"]["reverse"]["verified_objects"] == 5
        assert report["drivers"]["object"]["addressing_style"] == "path"
        assert report["drivers"]["object"]["chunk_size"] == 5_242_880
        assert report["drivers"]["object"]["multipart_threshold"] == 8_388_608
        assert report["summary"]["fault_scenarios"] == 4
        assert report["summary"]["recovered_fault_scenarios"] == 4
        assert report["summary"]["injected_failure_events"] == 6
    finally:
        drivers = load_drivers(str(drivers_path))
        s3 = drivers["object"]
        assert isinstance(s3, S3Driver)
        for key in list(s3.list_objects(bucket)):
            s3.delete_object(bucket, key)
        try:
            s3._client.delete_bucket(Bucket=bucket)
        except Exception:
            pass
