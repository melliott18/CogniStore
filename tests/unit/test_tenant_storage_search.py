from __future__ import annotations

import json
from base64 import urlsafe_b64decode
from io import BytesIO
from pathlib import Path

import pytest

from cognistore.auth.tenancy import TenantIsolationError, tenant_context
from cognistore.core.catalog import ObjectRecord
from cognistore.core.policy_features import CatalogPolicyFeatureLoader
from cognistore.core.tenant_dependencies import for_tenant
from cognistore.db.catalog import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.storage_driver import ObjectGenerationMismatchError
from cognistore.drivers.tenancy import TenantStorageDriver, scope_storage_drivers
from cognistore.search.ask import AskQuery, AskService
from cognistore.search.keyword import (
    KeywordSearchQuery,
    KeywordSearchService,
    project_catalog_record,
)
from cognistore.search.tantivy import MIN_WRITER_HEAP_BYTES, TantivyKeywordIndex


def test_storage_scopes_identical_keys_streams_generation_and_deletion(tmp_path: Path) -> None:
    raw = PosixDriver(tmp_path)
    first, second, legacy = [TenantStorageDriver(raw, tenant) for tenant in ("a", "b", "default")]
    legacy.put_object("bucket", "same", b"legacy")
    first.put_object_stream("bucket", "same", BytesIO(b"first"), size=5)
    second.put_object("bucket", "same", b"second")
    assert raw.get_object("bucket", "same") == b"legacy"
    assert first.get_object("bucket", "same", range="bytes=1-3") == b"irs"
    generation = first.object_generation("bucket", "same")
    with first.open_object_reader_if_generation("bucket", "same", generation) as reader:
        assert reader.read() == b"first"
    with pytest.raises(ObjectGenerationMismatchError):
        second.delete_object_if_generation("bucket", "same", generation)
    first.ensure_object_durable("bucket", "same")
    assert first.delete_object_if_generation("bucket", "same", generation)
    assert second.get_object("bucket", "same") == b"second"
    assert list(first.list_objects("bucket")) == []
    assert list(second.list_objects("bucket")) == ["same"]
    assert list(legacy.list_objects("bucket")) == ["same"]
    scoped = scope_storage_drivers({"hot": second}, "b")["hot"]
    assert scoped.get_object("bucket", "same") == b"second"
    assert first.same_backend(TenantStorageDriver(raw, "a"))
    assert not first.same_backend(second)


@pytest.mark.parametrize("key", ["../same", "/same", "x/../../same", "x\\..\\same", ".cognistore-tenants/secret", "x/./same"])
def test_storage_rejects_namespace_escapes(tmp_path: Path, key: str) -> None:
    raw = PosixDriver(tmp_path)
    for tenant in ("a", "default"):
        driver = TenantStorageDriver(raw, tenant)
        with pytest.raises(ValueError):
            driver.put_object("bucket", key, b"escape")
        with pytest.raises(ValueError):
            driver.get_object("bucket", key)
        with pytest.raises(ValueError):
            driver.list_objects_page("bucket", key)
        with pytest.raises(ValueError):
            driver.put_object("bucket/.cognistore-tenants/other", "same", b"escape")


def test_tenant_listing_cursors_are_scoped_and_default_inventory_hides_private_keys(tmp_path: Path) -> None:
    raw = PosixDriver(tmp_path)
    first = TenantStorageDriver(raw, "a")
    second = TenantStorageDriver(raw, "b")
    legacy = TenantStorageDriver(raw)
    for driver in (first, second, legacy):
        for key in ("prefix/one", "prefix/two", "three"):
            driver.put_object("bucket", key, driver.tenant_id.encode())
    page = first.list_objects_page("bucket", "prefix/", limit=1)
    assert page.keys == ("prefix/one",)
    assert page.next_cursor is not None
    with pytest.raises(ValueError):
        second.list_objects_page("bucket", "prefix/", cursor=page.next_cursor, limit=1)
    rebuilt = TenantStorageDriver(PosixDriver(tmp_path), "a")
    assert rebuilt.list_objects_page("bucket", "prefix/", cursor=page.next_cursor).keys == ("prefix/two",)
    for driver in (first, second, legacy):
        cursor = None
        keys = []
        while True:
            page = driver.list_objects_page("bucket", cursor=cursor, limit=1)
            assert len(page.keys) == 1
            keys.extend(page.keys)
            cursor = page.next_cursor
            if cursor is not None and driver is legacy:
                wrapper = json.loads(urlsafe_b64decode(cursor))
                backend = json.loads(urlsafe_b64decode(wrapper[4]))
                assert backend[4] == page.keys[-1]
            if cursor is None:
                break
        assert keys == ["prefix/one", "prefix/two", "three"]

    for key in ("prefix/one", "prefix/two", "three"):
        legacy.delete_object("bucket", key)
    empty = legacy.list_objects_page("bucket", limit=1)
    assert empty.keys == ()
    assert empty.next_cursor is None
    legacy.put_object("bucket", "!public-before-private-keys", b"public")
    last = legacy.list_objects_page("bucket", limit=1)
    assert last.keys == ("!public-before-private-keys",)
    assert last.next_cursor is None


def test_storage_handles_and_lazy_io_reject_another_active_tenant(tmp_path: Path) -> None:
    first = TenantStorageDriver(PosixDriver(tmp_path), "a")
    first.put_object("bucket", "same", b"first")
    reader = first.open_object_reader("bucket", "same")
    listing = first.list_objects("bucket")
    with tenant_context("b"):
        with pytest.raises(TenantIsolationError):
            first.get_object("bucket", "same")
        with pytest.raises(TenantIsolationError):
            with reader:
                pass
        with pytest.raises(TenantIsolationError):
            next(listing)
    with first.open_object_reader("bucket", "same") as opened:
        with tenant_context("b"), pytest.raises(TenantIsolationError):
            opened.read()


def test_keyword_indexes_keep_rebuild_delete_and_reopen_inside_tenant(tmp_path: Path) -> None:
    path = tmp_path / "index"
    with TantivyKeywordIndex(path, writer_heap_bytes=MIN_WRITER_HEAP_BYTES) as root:
        first, second = root.for_tenant("a"), root.for_tenant("b")
        assert root.for_tenant("a") is first
        first.replace_object(project_catalog_record(ObjectRecord("bucket", "same", 1, "hot", {"mime": "text/first"})))
        second.replace_object(project_catalog_record(ObjectRecord("bucket", "same", 2, "hot", {"mime": "text/second"})))
        assert first.search(KeywordSearchQuery())[0].size == 1
        assert second.search(KeywordSearchQuery())[0].size == 2
        assert root.search(KeywordSearchQuery()) == []
        first.rebuild([])
        assert first.search(KeywordSearchQuery()) == []
        assert second.search(KeywordSearchQuery())[0].size == 2
        with tenant_context("a"), pytest.raises(TenantIsolationError):
            second.search(KeywordSearchQuery())
        with tenant_context("a"), pytest.raises(TenantIsolationError):
            root.for_tenant("never-created")
    with TantivyKeywordIndex(path, tenant_id="b", writer_heap_bytes=MIN_WRITER_HEAP_BYTES) as reopened:
        assert reopened.search(KeywordSearchQuery())[0].size == 2
        reopened.delete_object("bucket", "same")
        assert reopened.search(KeywordSearchQuery()) == []


def test_ask_and_feature_loader_rebind_catalog_dependencies(tmp_path: Path) -> None:
    with SQLCatalog(tmp_path / "catalog.db") as root:
        first, second = root.for_tenant("a"), root.for_tenant("b")
        first.upsert("bucket", "same", 1, "hot", {"description": "alpha"})
        second.upsert("bucket", "same", 2, "hot", {"description": "beta"})
        ask = AskService(root)
        assert ask.for_tenant("a").ask(AskQuery("alpha")).results[0].citation.size == 1
        assert ask.for_tenant("b").ask(AskQuery("alpha")).results == ()
        assert ask.for_tenant("b").ask(AskQuery("beta")).results[0].citation.size == 2
        loader = CatalogPolicyFeatureLoader(access_catalog=root).for_tenant("b")
        assert loader.access_catalog is second
        assert loader.tenant_id == "b"
        with tenant_context("a"), pytest.raises(TenantIsolationError):
            loader.load([])


def test_unscopable_search_extension_fails_closed_for_nondefault_tenant(tmp_path: Path) -> None:
    class SharedKeyword:
        def search(self, query: KeywordSearchQuery) -> list:
            raise AssertionError("Shared provider must never be queried")

    with SQLCatalog(tmp_path / "catalog.db") as catalog:
        service = AskService(catalog, keyword=SharedKeyword())
        with pytest.raises(TenantIsolationError):
            service.for_tenant("a")
        with TantivyKeywordIndex(tmp_path / "index", writer_heap_bytes=MIN_WRITER_HEAP_BYTES) as index:
            keyword = KeywordSearchService(catalog, index).for_tenant("a")
            assert keyword.catalog.tenant_id == keyword.adapter.tenant_id == "a"


def test_extension_cannot_claim_to_scope_while_returning_shared_state() -> None:
    class SharedState:
        tenant_id = "default"

        def for_tenant(self, tenant_id: str) -> SharedState:
            return self

    with pytest.raises(TenantIsolationError):
        for_tenant(SharedState(), "a")
