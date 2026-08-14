"""Opt-in MinIO execution of the shared storage-driver conformance suite.

The tests are deliberately disabled unless all required MinIO settings are
present.  They never fall back to the ambient AWS credential chain.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import boto3
import pytest
from botocore.client import Config
from botocore.exceptions import ClientError

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


@pytest.fixture(scope="class")
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
        try:
            paginator = minio_client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket):
                for item in page.get("Contents", []):
                    minio_client.delete_object(Bucket=bucket, Key=item["Key"])
            minio_client.delete_bucket(Bucket=bucket)
        except ClientError as error:
            if not _is_not_found(error):
                raise
