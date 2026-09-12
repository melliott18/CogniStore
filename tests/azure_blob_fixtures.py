"""Stateful Azure SDK fake with committed and uncommitted blocks kept separate.

Only publication makes staged bytes visible. A successful Put Block List also
discards unlisted uncommitted blocks, as the real service does. The integration
suite exercises the same driver against Azurite rather than this protocol fake.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import pytest

pytest.importorskip("azure.storage.blob")

from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError
from azure.storage.blob import BlobProperties, ContentSettings


def azure_error(code: str, status: int, message: str | None = None) -> HttpResponseError:
    error = HttpResponseError(message=message or code)
    error.status_code = status
    error.error_code = code
    return error


class FakeAzureDownload:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.closed = False

    def readall(self) -> bytes:
        return self.data

    def close(self) -> None:
        self.closed = True


class FakeAzurePager:
    def __init__(self, service: "FakeAzureService", names: list[str], size: int) -> None:
        self.service = service
        self.names = names
        self.size = size

    def __iter__(self):
        for page in self.by_page():
            yield from page

    def by_page(self, continuation_token: str | None = None):
        start = int(continuation_token or 0)
        for offset in range(start, len(self.names), self.size):
            self.service.pages_read += 1
            self.service.fail_if_requested("list_page")
            yield iter(BlobProperties(name=name) for name in self.names[offset:offset + self.size])


class FakeAzureContainer:
    def __init__(self, service: "FakeAzureService", name: str) -> None:
        self.service = service
        self.name = name

    def create_container(self, **kwargs: Any) -> dict[str, Any]:
        self.service.create_calls.append({"container": self.name, **kwargs})
        self.service.fail_if_requested("create")
        if self.name in self.service.containers:
            raise azure_error("ContainerAlreadyExists", 409)
        self.service.containers.add(self.name)
        return {}

    def get_container_properties(self, **kwargs: Any) -> dict[str, Any]:
        self.service.require_container(self.name)
        return {}

    def list_blobs(self, name_starts_with: str = "", **kwargs: Any) -> FakeAzurePager:
        self.service.list_calls.append({
            "container": self.name, "name_starts_with": name_starts_with, **kwargs,
        })
        self.service.fail_if_requested("list")
        self.service.require_container(self.name)
        names = sorted(
            key for container, key in self.service.blobs
            if container == self.name and key.startswith(name_starts_with)
        )
        return FakeAzurePager(self.service, names, kwargs.get("results_per_page", 5000))


class FakeAzureBlob:
    def __init__(self, service: "FakeAzureService", container: str, key: str) -> None:
        self.service = service
        self.identity = (container, key)

    def _record(self, operation: str, kwargs: dict[str, Any]) -> None:
        getattr(self.service, operation + "_calls").append({
            "container": self.identity[0], "blob": self.identity[1], **kwargs,
        })
        self.service.fail_if_requested(operation)
        self.service.require_container(self.identity[0])

    def _existing(self) -> dict[str, Any]:
        if self.identity not in self.service.blobs:
            raise azure_error("BlobNotFound", 404)
        return self.service.blobs[self.identity]

    def _condition(self, kwargs: dict[str, Any]) -> None:
        existing = self.service.blobs.get(self.identity)
        condition = kwargs.get("match_condition")
        if condition == MatchConditions.IfMissing and existing is not None:
            raise azure_error("ConditionNotMet", 412)
        if condition == MatchConditions.IfNotModified:
            if existing is None:
                raise azure_error("BlobNotFound", 404)
            if kwargs.get("etag") != existing["etag"]:
                raise azure_error("ConditionNotMet", 412)

    def get_blob_properties(self, **kwargs: Any) -> BlobProperties:
        self._record("head", kwargs)
        self._condition(kwargs)
        value = self._existing()
        properties = BlobProperties(name=self.identity[1])
        properties.size = len(value["data"])
        properties.etag = value["etag"]
        properties.last_modified = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        properties.metadata = value.get("metadata", {})
        properties.content_settings = value.get("content_settings", ContentSettings())
        return properties

    def download_blob(
        self, offset: int | None = None, length: int | None = None, **kwargs: Any,
    ) -> FakeAzureDownload:
        self._record("get", {"offset": offset, "length": length, **kwargs})
        self._condition(kwargs)
        data = self._existing()["data"]
        start = offset or 0
        if start >= len(data) and data:
            raise azure_error("InvalidRange", 416)
        end = None if length is None else start + length
        result = FakeAzureDownload(data[start:end])
        self.service.downloads.append(result)
        return result

    def stage_block(self, block_id: str, data: bytes, length: int | None = None, **kwargs: Any):
        self._record("stage", {"block_id": block_id, "data": data, "length": length, **kwargs})
        assert length is None or length == len(data)
        self.service.staged[self.identity][block_id] = bytes(data)
        return {}

    def commit_block_list(self, block_list: list[Any], **kwargs: Any) -> dict[str, Any]:
        self._record("commit", {"block_list": list(block_list), **kwargs})
        self._condition(kwargs)
        ids = [block if isinstance(block, str) else block.id for block in block_list]
        staged = self.service.staged[self.identity]
        if any(block_id not in staged for block_id in ids):
            raise azure_error("InvalidBlockList", 400)
        payload = b"".join(staged[block_id] for block_id in ids)
        self.service.seed(*self.identity, payload, **kwargs)
        staged.clear()
        return {"etag": self._existing()["etag"]}

    def delete_blob(self, **kwargs: Any) -> None:
        self._record("delete", kwargs)
        self._condition(kwargs)
        self._existing()
        del self.service.blobs[self.identity]
        self.service.staged.pop(self.identity, None)


class FakeAzureService:
    url = "https://account.blob.core.windows.net/"

    def __init__(self) -> None:
        self.containers: set[str] = set()
        self.blobs: dict[tuple[str, str], dict[str, Any]] = {}
        self.staged: dict[tuple[str, str], dict[str, bytes]] = defaultdict(dict)
        self.errors: dict[str, BaseException] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.head_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.stage_calls: list[dict[str, Any]] = []
        self.commit_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.downloads: list[FakeAzureDownload] = []
        self.pages_read = 0
        self.generation = 0

    def fail_if_requested(self, operation: str) -> None:
        if operation in self.errors:
            raise self.errors[operation]

    def require_container(self, container: str) -> None:
        if container not in self.containers:
            raise azure_error("ContainerNotFound", 404)

    def seed(self, container: str, key: str, data: bytes, **kwargs: Any) -> None:
        self.containers.add(container)
        self.generation += 1
        self.blobs[(container, key)] = {
            "data": data, "etag": f'"generation-{self.generation}"',
            "metadata": kwargs.get("metadata", {}),
            "content_settings": kwargs.get("content_settings", ContentSettings()),
        }

    def get_blob_client(self, container: str, blob: str, **kwargs: Any) -> FakeAzureBlob:
        return FakeAzureBlob(self, container, blob)

    def get_container_client(self, container: str) -> FakeAzureContainer:
        return FakeAzureContainer(self, container)
