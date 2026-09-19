"""Azure protocol, failure atomicity, resource and bounded-memory regressions."""

from __future__ import annotations

import asyncio
import hashlib
import traceback
import tracemalloc
from io import BytesIO
from typing import Any

import pytest

pytest.importorskip("azure.storage.blob")

from azure.core import MatchConditions
from azure.core.exceptions import ServiceRequestError

from cognistore.drivers import azure_blob_driver
from cognistore.drivers.azure_blob_driver import AzureBlobDriver, AzureBlobError
from cognistore.drivers.storage_driver import ObjectGenerationMismatchError
from cognistore.jobs.retry import FailureCategory, classify_job_error
from tests.azure_blob_fixtures import FakeAzureBlob, FakeAzureService, azure_error


@pytest.fixture
def client() -> FakeAzureService:
    service = FakeAzureService()
    service.seed("container", "key", b"0123456789")
    return service


def test_capabilities_match_atomic_blob_protocol(client: FakeAzureService) -> None:
    driver = AzureBlobDriver(client=client)

    assert driver.capabilities.range_reads is True
    assert driver.capabilities.range_writes is False
    assert driver.capabilities.atomic_no_overwrite is True
    assert driver.capabilities.conditional_delete is True


def test_range_write_is_rejected_before_mutating_storage(client: FakeAzureService) -> None:
    driver = AzureBlobDriver(client=client)

    with pytest.raises(NotImplementedError):
        driver.put_object("container", "key", b"xx", range="bytes=0-1")

    assert client.stage_calls == client.commit_calls == []
    assert client.blobs[("container", "key")]["data"] == b"0123456789"


@pytest.mark.parametrize(
    ("range_header", "expected"),
    [
        ("bytes=2-6", b"23456"),
        ("bytes=7-", b"789"),
        ("bytes=-3", b"789"),
        ("bytes=7-99", b"789"),
        ("bytes=-99", b"0123456789"),
    ],
)
def test_ranges_translate_to_bounded_azure_offsets(
    client: FakeAzureService, range_header: str, expected: bytes,
) -> None:
    driver = AzureBlobDriver(client=client, chunk_size=3)

    assert driver.get_object("container", "key", range=range_header) == expected

    assert client.get_calls
    assert all(0 < call["length"] <= 3 for call in client.get_calls)
    assert all(call["max_concurrency"] == 1 for call in client.get_calls)


@pytest.mark.parametrize(
    "range_header",
    ["", "items=0-1", "bytes=2-1", "bytes=-0", "bytes=a-2", "bytes=0-1,3-4"],
)
def test_invalid_ranges_fail_without_downloading(
    client: FakeAzureService, range_header: str,
) -> None:
    with pytest.raises(ValueError):
        AzureBlobDriver(client=client).get_object("container", "key", range=range_header)

    assert client.get_calls == []


def test_reader_fetches_lazily_and_never_downloads_an_entire_large_blob(
    client: FakeAzureService,
) -> None:
    driver = AzureBlobDriver(client=client, chunk_size=3)

    with driver.open_object_reader("container", "key") as source:
        assert len(client.get_calls) <= 1
        assert source.read(1) == b"0"
        assert len(client.get_calls) == 1
        assert source.read(0) == b""
        assert len(client.get_calls) == 1
        assert source.read(3) == b"123"
        assert source.read() == b"456789"
        count_at_eof = len(client.get_calls)
        assert source.read(2) == b""
        assert len(client.get_calls) == count_at_eof

    assert all(call["length"] <= 3 for call in client.get_calls)


def test_generation_bound_reader_applies_etag_to_every_download(
    client: FakeAzureService,
) -> None:
    driver = AzureBlobDriver(client=client, chunk_size=3)
    generation = driver.object_generation("container", "key")

    with driver.open_object_reader_if_generation("container", "key", generation) as source:
        assert source.read(3) == b"012"
        client.seed("container", "key", b"replacement")
        with pytest.raises(ObjectGenerationMismatchError):
            source.read(3)

    assert len(client.get_calls) == 2
    assert all(call["etag"] == generation for call in client.get_calls)
    assert all(
        call["match_condition"] == MatchConditions.IfNotModified for call in client.get_calls
    )


def test_generation_bound_open_rejects_race_after_properties(client: FakeAzureService) -> None:
    driver = AzureBlobDriver(client=client)
    generation = driver.object_generation("container", "key")
    client.errors["get"] = azure_error("ConditionNotMet", 412)

    with pytest.raises(ObjectGenerationMismatchError):
        with driver.open_object_reader_if_generation("container", "key", generation):
            pytest.fail("a failed conditional request yielded a stream")


def test_stat_retains_metadata_size_timestamp_and_generation(client: FakeAzureService) -> None:
    driver = AzureBlobDriver(client=client)
    driver.put_object_stream(
        "container", "metadata", BytesIO(b"hello"), size=5,
        metadata={"content_type": "text/plain", "metadata": {"owner": "test"}},
    )

    call = client.commit_calls[-1]
    assert call["content_settings"].content_type == "text/plain"
    assert call["metadata"] == {"owner": "test"}
    value = driver.stat_object("container", "metadata")
    assert value["size"] == 5
    assert value["mtime"] == pytest.approx(1735787045.0)
    assert value["generation"] == client.blobs[("container", "metadata")]["etag"]


@pytest.mark.parametrize("method", ["get_object", "stat_object"])
def test_missing_container_or_blob_maps_to_file_not_found(
    client: FakeAzureService, method: str,
) -> None:
    driver = AzureBlobDriver(client=client)

    with pytest.raises(FileNotFoundError):
        getattr(driver, method)("container", "missing")
    with pytest.raises(FileNotFoundError):
        getattr(driver, method)("missing-container", "key")


def test_missing_delete_is_idempotent(client: FakeAzureService) -> None:
    driver = AzureBlobDriver(client=client)

    driver.delete_object("missing-container", "missing")
    driver.delete_object("container", "missing")
    driver.delete_object("container", "key")
    driver.delete_object("container", "key")


def test_conditional_delete_uses_atomic_etag_and_preserves_replacement(
    client: FakeAzureService,
) -> None:
    driver = AzureBlobDriver(client=client)
    original = driver.object_generation("container", "key")
    client.seed("container", "key", b"replacement")

    with pytest.raises(ObjectGenerationMismatchError):
        driver.delete_object_if_generation("container", "key", original)

    assert client.blobs[("container", "key")]["data"] == b"replacement"
    call = client.delete_calls[-1]
    assert call["etag"] == original
    assert call["match_condition"] == MatchConditions.IfNotModified
    current = driver.object_generation("container", "key")
    assert driver.delete_object_if_generation("container", "key", current)
    assert not driver.delete_object_if_generation("container", "key", current)


@pytest.mark.parametrize("generation", ["", None, True, 2])
def test_conditional_delete_rejects_invalid_generation(
    client: FakeAzureService, generation: Any,
) -> None:
    with pytest.raises(ValueError, match="generation.*non-empty string"):
        AzureBlobDriver(client=client).delete_object_if_generation("container", "key", generation)

    assert client.delete_calls == []


def test_conditional_delete_handles_azure_412_for_a_missing_blob(
    client: FakeAzureService,
) -> None:
    client.errors["delete"] = azure_error("ConditionNotMet", 412)

    assert not AzureBlobDriver(client=client).delete_object_if_generation(
        "container", "missing", '"deleted-generation"',
    )


def test_reader_is_closed_when_its_consumer_raises(client: FakeAzureService) -> None:
    driver = AzureBlobDriver(client=client, chunk_size=3)

    with pytest.raises(RuntimeError, match="consumer failed"):
        with driver.open_object_reader("container", "key") as source:
            assert source.read(1) == b"0"
            raise RuntimeError("consumer failed")

    assert source.closed
    with pytest.raises(ValueError, match="closed"):
        source.read(1)
    assert len(client.get_calls) == 1


def test_partial_provider_range_is_not_accepted_as_a_successful_read(
    client: FakeAzureService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.azure_blob_fixtures import FakeAzureDownload

    monkeypatch.setattr(FakeAzureDownload, "readall", lambda self: b"x")

    with pytest.raises(OSError, match="incomplete range"):
        AzureBlobDriver(client=client, chunk_size=3).get_object("container", "key")


@pytest.mark.parametrize(
    ("status", "expected_retry", "expected_category"),
    [
        (400, False, FailureCategory.INVALID),
        (429, True, FailureCategory.THROTTLED),
        (503, True, FailureCategory.UNAVAILABLE),
        (None, True, FailureCategory.UNKNOWN),
    ],
)
def test_normalized_failures_retain_worker_retry_semantics(
    status: int | None, expected_retry: bool, expected_category: FailureCategory,
) -> None:
    error = AzureBlobError("read", status_code=status, error_code=None)

    classification = classify_job_error(error)

    assert classification.retryable is expected_retry
    assert classification.category == expected_category


def test_untrusted_error_code_cannot_disclose_a_signed_url(client: FakeAzureService) -> None:
    client.errors["head"] = azure_error("https://host.invalid?sig=secret-token", 500)

    with pytest.raises(AzureBlobError) as captured:
        AzureBlobDriver(client=client).stat_object("container", "key")

    assert captured.value.error_code is None
    assert "secret-token" not in str(captured.value)


def test_listing_traverses_pages_and_filters_prefix(client: FakeAzureService) -> None:
    for key in ["alpha/one", "alpha/two", "alpha/three", "beta/four"]:
        client.seed("container", key, b"data")
    driver = AzureBlobDriver(client=client, list_page_size=2)

    assert list(driver.list_objects("container", prefix="alpha/")) == [
        "alpha/one", "alpha/three", "alpha/two",
    ]

    assert client.pages_read == 2
    assert client.list_calls == [{
        "container": "container", "name_starts_with": "alpha/", "results_per_page": 2,
        "logging_enable": False,
    }]


def test_auto_create_container_only_mutates_on_writes() -> None:
    client = FakeAzureService()
    driver = AzureBlobDriver(client=client, auto_create_container=True)

    with pytest.raises(FileNotFoundError):
        driver.get_object("new-container", "key")
    assert client.create_calls == []

    driver.put_object("new-container", "key", b"hello")
    assert len(client.create_calls) == 1
    assert driver.get_object("new-container", "key") == b"hello"


def test_authorization_failure_does_not_trigger_container_creation(client: FakeAzureService) -> None:
    client.errors["stage"] = azure_error("AuthorizationFailure", 403)

    with pytest.raises((PermissionError, AzureBlobError)):
        AzureBlobDriver(client=client, auto_create_container=True).put_object(
            "container", "key", b"hello",
        )

    assert client.create_calls == []
    assert client.commit_calls == []


@pytest.mark.parametrize(("status", "code"), [(429, "ServerBusy"), (503, "ServerBusy")])
@pytest.mark.parametrize("operation", ["head", "get", "stage", "commit", "delete", "list_page"])
def test_exhausted_throttling_is_normalized_and_never_leaks_credentials(
    client: FakeAzureService, status: int, code: str, operation: str,
) -> None:
    secret = "SECRET-SAS-signature"
    client.errors[operation] = azure_error(
        code, status, f"GET https://account.blob.core.windows.net/c/k?sig={secret}",
    )
    driver = AzureBlobDriver(client=client)

    with pytest.raises(AzureBlobError) as captured:
        if operation == "list_page":
            list(driver.list_objects("container"))
        elif operation == "delete":
            driver.delete_object("container", "key")
        elif operation in {"stage", "commit"}:
            driver.put_object("container", "key", b"new")
        else:
            driver.get_object("container", "key")

    error = captured.value
    assert error.status_code == status
    assert error.error_code == code
    assert secret not in "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert secret not in repr(error)


def test_transport_failure_does_not_expose_signed_request_url(client: FakeAzureService) -> None:
    client.errors["get"] = ServiceRequestError("https://account.example?sig=secret-token")

    with pytest.raises(AzureBlobError) as captured:
        AzureBlobDriver(client=client).get_object("container", "key")

    error = captured.value
    assert "secret-token" not in "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )


class ReadTrackingStream(BytesIO):
    def __init__(self, payload: bytes, fragment_size: int | None = None) -> None:
        super().__init__(payload)
        self.requests: list[int] = []
        self.fragment_size = fragment_size

    def read(self, size: int = -1) -> bytes:
        self.requests.append(size)
        assert size > 0, "the driver must never request the whole source"
        return super().read(min(size, self.fragment_size) if self.fragment_size else size)


def test_short_reads_produce_ordered_bounded_blocks_and_atomic_conditional_commit(
    client: FakeAzureService,
) -> None:
    driver = AzureBlobDriver(client=client, chunk_size=4)
    source = ReadTrackingStream(b"hello-world", fragment_size=1)

    assert driver.put_object_stream(
        "container", "new", source, size=11, overwrite=False,
    ) == 11

    assert [call["data"] for call in client.stage_calls] == [b"hell", b"o-wo", b"rld"]
    assert all(0 < amount <= 4 for amount in source.requests)
    assert source.requests[-1] == 1
    assert not source.closed
    commit = client.commit_calls[0]
    ids = [block if isinstance(block, str) else block.id for block in commit["block_list"]]
    assert ids == [call["block_id"] for call in client.stage_calls]
    # The SDK translates IfMissing to If-None-Match: *. An additional `etag`
    # argument is not consumed for this condition and leaks into the transport.
    assert "etag" not in commit
    assert commit["match_condition"] == MatchConditions.IfMissing
    assert client.blobs[("container", "new")]["data"] == b"hello-world"
    assert len({len(block_id) for block_id in ids}) == 1


@pytest.mark.parametrize(("payload", "size"), [(b"short", 6), (b"too-long", 7), (b"x", 0)])
@pytest.mark.parametrize("existing", [True, False])
def test_size_mismatch_never_commits_or_deletes_any_generation(
    client: FakeAzureService, payload: bytes, size: int, existing: bool,
) -> None:
    if not existing:
        client.blobs.clear()
    before = dict(client.blobs)

    with pytest.raises(ValueError):
        AzureBlobDriver(client=client, chunk_size=4).put_object_stream(
            "container", "key", BytesIO(payload), size=size,
        )

    assert client.blobs == before
    assert client.commit_calls == client.delete_calls == []


@pytest.mark.parametrize("failure", [OSError("source failed"), asyncio.CancelledError()])
def test_interrupted_source_leaves_staged_blocks_unpublished_without_deleting_object(
    client: FakeAzureService, failure: BaseException,
) -> None:
    class FailingSource:
        reads = 0

        def read(self, size: int = -1) -> bytes:
            self.reads += 1
            if self.reads == 1:
                return b"x" * size
            raise failure

    before = dict(client.blobs)

    with pytest.raises(type(failure)):
        AzureBlobDriver(client=client, chunk_size=4).put_object_stream(
            "container", "key", FailingSource(), size=8,
        )

    assert len(client.stage_calls) == 1
    assert client.commit_calls == client.delete_calls == []
    assert client.blobs == before


def test_source_returning_more_than_requested_is_rejected(client: FakeAzureService) -> None:
    class OversizedRead:
        def read(self, size: int = -1) -> bytes:
            return b"x" * (size + 1)

    with pytest.raises(ValueError):
        AzureBlobDriver(client=client, chunk_size=4).put_object_stream(
            "container", "key", OversizedRead(), size=4,
        )

    assert client.commit_calls == client.delete_calls == []
    assert client.blobs[("container", "key")]["data"] == b"0123456789"


@pytest.mark.parametrize("size", [True, -1, 1.5, "4"])
def test_invalid_size_rejected_before_any_provider_write(client: FakeAzureService, size: Any) -> None:
    with pytest.raises(ValueError, match="size"):
        AzureBlobDriver(client=client).put_object_stream("container", "key", BytesIO(), size=size)

    assert client.stage_calls == client.commit_calls == []


@pytest.mark.parametrize("chunk_size", [True, 0, -1, 1.5, "4"])
def test_invalid_chunk_size_rejected(client: FakeAzureService, chunk_size: Any) -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        AzureBlobDriver(client=client, chunk_size=chunk_size)


@pytest.mark.parametrize("value", [None, "text", bytearray(b"data")])
def test_nonbinary_stream_rejected_without_publication(client: FakeAzureService, value: Any) -> None:
    class NonBinaryStream:
        def read(self, size: int = -1) -> Any:
            return value

    with pytest.raises(TypeError, match="bytes"):
        AzureBlobDriver(client=client).put_object_stream(
            "container", "key", NonBinaryStream(), size=4,
        )

    assert client.stage_calls == client.commit_calls == client.delete_calls == []


def test_more_than_fifty_thousand_blocks_rejected_before_read_or_write(
    client: FakeAzureService,
) -> None:
    source = ReadTrackingStream(b"")

    with pytest.raises(ValueError):
        AzureBlobDriver(client=client, chunk_size=4).put_object_stream(
            "container", "key", source, size=4 * 50_000 + 1,
        )

    assert source.requests == []
    assert client.stage_calls == client.commit_calls == []


def test_failed_stage_never_publishes_or_deletes_existing_data(client: FakeAzureService) -> None:
    client.errors["stage"] = azure_error("ServerBusy", 503)

    with pytest.raises(AzureBlobError):
        AzureBlobDriver(client=client, chunk_size=4).put_object_stream(
            "container", "key", BytesIO(b"replacement"), size=11,
        )

    assert client.commit_calls == client.delete_calls == []
    assert client.blobs[("container", "key")]["data"] == b"0123456789"


def test_commit_conflict_preserves_winner_and_does_not_attempt_destructive_cleanup(
    client: FakeAzureService,
) -> None:
    class ConcurrentWriter(BytesIO):
        def read(self, size: int = -1) -> bytes:
            data = super().read(size)
            if not data:
                client.seed("container", "new", b"concurrent-winner")
            return data

    with pytest.raises(FileExistsError):
        AzureBlobDriver(client=client, chunk_size=4).put_object_stream(
            "container", "new", ConcurrentWriter(b"loser"), size=5, overwrite=False,
        )

    assert client.blobs[("container", "new")]["data"] == b"concurrent-winner"
    assert len(client.commit_calls) == 1
    assert client.delete_calls == []


def test_each_upload_uses_unique_block_ids_so_retries_cannot_mix_data(
    client: FakeAzureService,
) -> None:
    driver = AzureBlobDriver(client=client, chunk_size=4)
    with pytest.raises(ValueError):
        driver.put_object_stream("container", "new", BytesIO(b"old-data"), size=9)
    abandoned_ids = {call["block_id"] for call in client.stage_calls}
    assert abandoned_ids
    client.stage_calls.clear()

    driver.put_object_stream("container", "new", BytesIO(b"fresh!!!"), size=8)

    fresh_ids = {call["block_id"] for call in client.stage_calls}
    assert abandoned_ids.isdisjoint(fresh_ids)
    assert client.blobs[("container", "new")]["data"] == b"fresh!!!"


def test_stream_peak_memory_is_chunk_bounded_independently_of_object_size() -> None:
    class GeneratedSource:
        def __init__(self, size: int) -> None:
            self.remaining = size

        def read(self, size: int = -1) -> bytes:
            assert size > 0
            count = min(size, self.remaining)
            self.remaining -= count
            return b"x" * count

    class DiscardingBlob(FakeAzureBlob):
        def stage_block(
            self, block_id: str, data: bytes, length: int | None = None, **kwargs: Any,
        ) -> dict[str, Any]:
            assert length == len(data)
            assert length <= chunk_size
            digest.update(data)
            return {}

        def commit_block_list(self, block_list: list[Any], **kwargs: Any) -> dict[str, Any]:
            assert len(block_list) == 40
            return {"etag": '"complete"'}

    class DiscardingService(FakeAzureService):
        def get_blob_client(self, container: str, blob: str, **kwargs: Any) -> DiscardingBlob:
            return DiscardingBlob(self, container, blob)

    chunk_size = 256 * 1024
    total = 40 * chunk_size
    digest = hashlib.sha256()
    driver = AzureBlobDriver(client=DiscardingService(), chunk_size=chunk_size)

    tracemalloc.start()
    try:
        assert driver.put_object_stream("container", "large", GeneratedSource(total), size=total) == total
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert digest.digest() == hashlib.sha256(b"x" * total).digest()
    assert peak < 8 * chunk_size


def test_default_identity_and_sdk_connections_are_owned_and_closed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class OwnedCredential:
        closed = 0

        def close(self) -> None:
            self.closed += 1

    class OwnedClient(FakeAzureService):
        closed = 0

        def close(self) -> None:
            self.closed += 1

    credential = OwnedCredential()
    client = OwnedClient()

    def make_client(account_url: str, **kwargs: Any) -> OwnedClient:
        calls.append({"account_url": account_url, **kwargs})
        return client

    credential_options: dict[str, Any] = {}

    def make_credential(**kwargs: Any) -> OwnedCredential:
        credential_options.update(kwargs)
        return credential

    monkeypatch.setattr(azure_blob_driver, "DefaultAzureCredential", make_credential)
    monkeypatch.setattr(azure_blob_driver, "BlobServiceClient", make_client)

    driver = AzureBlobDriver(account_url=client.url, chunk_size=4096)
    driver.close()
    driver.close()

    assert client.closed == credential.closed == 1
    assert calls[0]["credential"] is credential
    assert calls[0]["max_single_get_size"] == calls[0]["max_chunk_get_size"] == 4096
    assert calls[0]["max_block_size"] == 4096
    assert calls[0]["logging_enable"] is False
    assert calls[0]["retry_total"] == 3
    assert calls[0]["connection_verify"] is True
    assert credential_options["connection_verify"] is True


def test_explicit_sdk_client_lifetime_stays_with_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeAzureService()

    def unexpected_close() -> None:
        pytest.fail("driver closed an externally owned client")

    monkeypatch.setattr(client, "close", unexpected_close, raising=False)

    AzureBlobDriver(client=client).close()


def test_sas_url_does_not_initialize_default_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def forbidden_identity() -> None:
        pytest.fail("SAS authentication must not create a default identity")

    def make_client(account_url: str, **kwargs: Any) -> FakeAzureService:
        calls.append({"account_url": account_url, **kwargs})
        return FakeAzureService()

    monkeypatch.setattr(azure_blob_driver, "DefaultAzureCredential", forbidden_identity)
    monkeypatch.setattr(azure_blob_driver, "BlobServiceClient", make_client)

    driver = AzureBlobDriver(account_url="https://account.blob.core.windows.net/?sig=secret-token")

    assert calls[0]["credential"] is None
    assert calls[0]["account_url"].endswith("?sig=secret-token")
    assert driver.account_url == "https://account.blob.core.windows.net"
    assert "secret-token" not in repr(driver)


def test_connection_string_sdk_setup_errors_do_not_leak_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_connection(*args: Any, **kwargs: Any) -> None:
        raise ValueError("Invalid connection string: AccountKey=secret-account-key")

    monkeypatch.setattr(
        azure_blob_driver.BlobServiceClient, "from_connection_string", reject_connection,
    )
    connection_string = "AccountKey=secret-account-key"

    with pytest.raises(ValueError, match="Could not initialize Azure Blob client") as captured:
        AzureBlobDriver(connection_string=connection_string)

    error = captured.value
    assert "secret-account-key" not in "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )


def test_backend_identity_normalizes_endpoint_and_ignores_sas_credentials() -> None:
    first = AzureBlobDriver(
        client=FakeAzureService(), account_url="https://ACCOUNT.blob.core.windows.net/?sig=first",
    )
    second = AzureBlobDriver(
        client=FakeAzureService(), account_url="https://account.blob.core.windows.net?sig=second",
    )
    other = AzureBlobDriver(
        client=FakeAzureService(), account_url="https://other.blob.core.windows.net",
    )
    azurite_one = AzureBlobDriver(
        client=FakeAzureService(), account_url="http://localhost:10000/account-one",
    )
    azurite_two = AzureBlobDriver(
        client=FakeAzureService(), account_url="http://localhost:10000/account-two",
    )

    assert first.same_backend(second)
    assert not first.same_backend(other)
    assert not azurite_one.same_backend(azurite_two)
