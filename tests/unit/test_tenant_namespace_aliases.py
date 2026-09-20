"""Reserved storage namespaces cannot be reached through case aliases."""

from __future__ import annotations

import json
from base64 import urlsafe_b64decode
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.storage_driver import StorageDriver, StorageListingPage
from cognistore.drivers.tenancy import TenantStorageDriver

TENANT_ALIASES = (
    ".cognistore-tenants",
    ".COGNISTORE-TENANTS",
    ".CoGnIsToRe-TeNaNtS",
    ".cogniſtore-tenantſ",
    ".cogni\u200cstore-tenants",
    "\ufeff.cognistore-tenants",
    ".cognistore-tenants\ufeff",
    ".cogni\u00adstore-tenants",
    ".cogni\u034fstore-tenants",
    ".cognistore-tenants\ufe0f",
    ".cognistore-tenants\U000e0100",
)
OPERATIONS = (
    "put_object",
    "put_object_range",
    "put_object_stream",
    "get_object",
    "open_object_reader",
    "open_object_reader_if_generation",
    "stat_object",
    "object_generation",
    "ensure_object_durable",
    "delete_object",
    "delete_object_if_generation",
    "list_objects",
    "list_objects_page",
)


@pytest.mark.parametrize("ignored", [
    # Every Unicode 17 Default_Ignorable_Code_Point range endpoint, together
    # with every member of the original HFS+ ignorable set.
    "\u00ad", "\u034f", "\u061c", "\u115f", "\u1160", "\u17b4", "\u17b5",
    "\u180b", "\u180f", "\u200b",
    "\u200c", "\u200d", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2060", "\u206a", "\u206b", "\u206c", "\u206d", "\u206e", "\u206f",
    "\u3164", "\ufe00", "\ufe0f", "\ufeff", "\uffa0", "\ufff0", "\ufff8",
    "\U0001bca0", "\U0001bca3", "\U0001d173", "\U0001d17a", "\U000e0000", "\U000e0fff",
])
def test_unicode_ignorable_aliases_are_reserved_at_each_component_position(
    tmp_path: Path, ignored: str,
) -> None:
    raw = create_autospec(StorageDriver, instance=True)
    tenant = TenantStorageDriver(raw)
    posix = PosixDriver(str(tmp_path))
    for offset in (0, 7, 19):
        reserved = ".COGNISTORE-TENANTS"
        alias = reserved[:offset] + ignored + reserved[offset:]
        with pytest.raises(ValueError):
            tenant.put_object("bucket", alias + "/private.txt", b"replacement")
        with pytest.raises(ValueError):
            tenant.put_object(alias, "private.txt", b"replacement")
        reserved = ".COGNISTORE-STAGING"
        alias = reserved[:offset] + ignored + reserved[offset:]
        with pytest.raises(ValueError):
            posix.put_object(alias, "upload.tmp", b"replacement")
    assert raw.mock_calls == []
    assert list(tmp_path.iterdir()) == []


def _invoke(driver: StorageDriver, operation: str, bucket: str, key: str) -> None:
    """Exercise lazy readers and listings as well as immediate operations."""
    if operation == "put_object":
        driver.put_object(bucket, key, b"replacement")
    elif operation == "put_object_range":
        driver.put_object(bucket, key, b"replacement", range="bytes=0-1")
    elif operation == "put_object_stream":
        driver.put_object_stream(bucket, key, BytesIO(b"replacement"), size=11)
    elif operation == "open_object_reader":
        with driver.open_object_reader(bucket, key) as reader:
            reader.read()
    elif operation == "open_object_reader_if_generation":
        with driver.open_object_reader_if_generation(bucket, key, "generation") as reader:
            reader.read()
    elif operation == "delete_object_if_generation":
        driver.delete_object_if_generation(bucket, key, "generation")
    elif operation == "list_objects":
        list(driver.list_objects(bucket, key))
    elif operation == "list_objects_page":
        driver.list_objects_page(bucket, key, limit=1)
    else:
        getattr(driver, operation)(bucket, key)


@pytest.mark.parametrize("tenant_id", ["default", "team-a"])
@pytest.mark.parametrize("operation", OPERATIONS)
def test_reserved_key_aliases_are_rejected_before_backend_delegation(
    tenant_id: str, operation: str,
) -> None:
    raw = create_autospec(StorageDriver, instance=True)
    driver = TenantStorageDriver(raw, tenant_id)
    for alias in TENANT_ALIASES:
        for suffix in ("", "/", "/victim/private.txt"):
            with pytest.raises(ValueError, match="inside its namespace"):
                _invoke(driver, operation, "bucket", alias + suffix)
            assert raw.mock_calls == []


@pytest.mark.parametrize("tenant_id", ["default", "team-a"])
@pytest.mark.parametrize("operation", OPERATIONS)
def test_reserved_bucket_aliases_are_rejected_before_backend_delegation(
    tenant_id: str, operation: str,
) -> None:
    raw = create_autospec(StorageDriver, instance=True)
    driver = TenantStorageDriver(raw, tenant_id)
    for alias in TENANT_ALIASES:
        with pytest.raises(ValueError, match="single relative path component"):
            _invoke(driver, operation, alias, "private.txt")
        assert raw.mock_calls == []


@pytest.mark.parametrize("tenant_id", ["default", "team-a"])
@pytest.mark.parametrize("operation", OPERATIONS)
def test_ignorable_empty_dot_and_dotdot_components_are_rejected_before_delegation(
    tenant_id: str, operation: str,
) -> None:
    raw = create_autospec(StorageDriver, instance=True)
    driver = TenantStorageDriver(raw, tenant_id)
    for ignored in ("\u00ad", "\u034f", "\u200c", "\ufe0f", "\ufeff", "\U000e0100"):
        for component in (ignored, f".{ignored}", f".{ignored}."):
            with pytest.raises(ValueError):
                _invoke(driver, operation, component, "private.txt")
            for key in (
                component, f"{component}/private.txt", f"ordinary/{component}",
                f"ordinary/{component}/private.txt",
            ):
                with pytest.raises(ValueError):
                    _invoke(driver, operation, "bucket", key)
            assert raw.mock_calls == []


@pytest.mark.parametrize("tenant_id", ["default", "team-a"])
@pytest.mark.parametrize("operation", OPERATIONS)
def test_literal_empty_cloud_key_components_are_preserved(
    tenant_id: str, operation: str,
) -> None:
    raw = create_autospec(StorageDriver, instance=True)
    driver = TenantStorageDriver(raw, tenant_id)
    prefix = (
        "" if tenant_id == "default"
        else f".cognistore-tenants/{sha256(tenant_id.encode()).hexdigest()}/"
    )
    method = "put_object" if operation == "put_object_range" else operation
    for key in ("ordinary//private.txt", "ordinary/", "ordinary///private.txt"):
        physical = prefix + key
        raw.list_objects.return_value = (item for item in (physical,))
        raw.list_objects_page.return_value = StorageListingPage((physical,), None)
        _invoke(driver, operation, "bucket", key)
        called = getattr(raw, method)
        called.assert_called_once()
        assert called.call_args.args[:2] == ("bucket", physical)
        raw.reset_mock()


@pytest.mark.parametrize("tenant_id", ["default", "team-a"])
@pytest.mark.parametrize("alias", TENANT_ALIASES)
@pytest.mark.parametrize("visible_keys", [(), ("!public",), ("!public", "z-public")])
def test_listings_hide_raw_aliases_without_leaking_a_private_cursor(
    tmp_path: Path, tenant_id: str, alias: str, visible_keys: tuple[str, ...],
) -> None:
    physical_prefix = (
        "" if tenant_id == "default"
        else f".cognistore-tenants/{sha256(tenant_id.encode()).hexdigest()}/"
    )
    for key in (*visible_keys, f"{alias}/victim/private.txt"):
        path = tmp_path / "bucket" / (physical_prefix + key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"content")
    driver = TenantStorageDriver(PosixDriver(str(tmp_path)), tenant_id)
    assert sorted(driver.list_objects("bucket")) == list(visible_keys)
    # A partial namespace prefix is a valid literal prefix, but must not expose
    # reserved objects even when a backend returns them.
    partial_prefix = alias[:len(alias) // 2]
    assert list(driver.list_objects("bucket", partial_prefix)) == []
    filtered = driver.list_objects_page("bucket", partial_prefix, limit=1)
    assert filtered.keys == ()
    assert filtered.next_cursor is None

    cursor = None
    found = []
    for _ in range(len(visible_keys) + 1):
        page = driver.list_objects_page("bucket", cursor=cursor, limit=1)
        found.extend(page.keys)
        cursor = page.next_cursor
        if cursor is None:
            break
        wrapper = json.loads(urlsafe_b64decode(cursor))
        backend = json.loads(urlsafe_b64decode(wrapper[4]))
        assert backend[4] == physical_prefix + page.keys[-1]
    else:
        pytest.fail("listing did not terminate after all visible objects")
    assert found == list(visible_keys)


@pytest.mark.parametrize("tenant_id", ["default", "team-a"])
@pytest.mark.parametrize("key", [
    "Reports/CaseSensitive.TXT",
    ".COGNISTORE-TENANTS-backup/private.txt",
    "ordinary/.COGNISTORE-TENANTS/private.txt",
    "ordinary/.cogniſtore-tenantſ/private.txt",
    "ordi\u200cnary/Report.TXT",
    "\ufeffordinary/Report.TXT",
    "ordi\u00adnary/Report.TXT",
    "ordinary/Report\ufe0f.TXT",
])
def test_ordinary_keys_preserve_spelling_and_support_the_storage_lifecycle(
    tmp_path: Path, tenant_id: str, key: str,
) -> None:
    driver = TenantStorageDriver(PosixDriver(str(tmp_path)), tenant_id)
    driver.put_object("MixedCaseBucket", key, b"first")
    assert driver.get_object("MixedCaseBucket", key) == b"first"
    assert driver.put_object_stream("MixedCaseBucket", key, BytesIO(b"second"), size=6) == 6
    generation = driver.object_generation("MixedCaseBucket", key)
    assert driver.stat_object("MixedCaseBucket", key)["generation"] == generation
    with driver.open_object_reader("MixedCaseBucket", key) as reader:
        assert reader.read() == b"second"
    with driver.open_object_reader_if_generation("MixedCaseBucket", key, generation) as reader:
        assert reader.read() == b"second"
    driver.ensure_object_durable("MixedCaseBucket", key)
    assert list(driver.list_objects("MixedCaseBucket", key)) == [key]
    assert driver.list_objects_page("MixedCaseBucket", key, limit=1).keys == (key,)
    assert driver.delete_object_if_generation("MixedCaseBucket", key, generation)
    driver.put_object("MixedCaseBucket", key, b"third")
    driver.delete_object("MixedCaseBucket", key)
    assert driver.list_objects_page("MixedCaseBucket").keys == ()


@pytest.mark.parametrize("operation", OPERATIONS)
def test_posix_staging_bucket_aliases_are_rejected_before_filesystem_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    staging = tmp_path / ".cognistore-staging"
    staging.mkdir()
    sentinel = staging / "upload-protected.tmp"
    sentinel.write_bytes(b"private staging content")
    original = sentinel.stat()
    driver = PosixDriver(str(tmp_path))

    def forbidden(*args, **kwargs):
        pytest.fail("reserved bucket reached filesystem traversal")

    monkeypatch.setattr(driver, "_directory", forbidden)
    for alias in (
        ".cognistore-staging",
        ".COGNISTORE-STAGING",
        ".CoGnIsToRe-StAgInG",
        ".cogniſtore-ſtaging",
        ".cogni\u200cstore-staging",
        "\ufeff.cognistore-staging",
        ".cognistore-staging\ufeff",
        ".cogni\u00adstore-staging",
        ".cogni\u034fstore-staging",
        ".cognistore-staging\ufe0f",
        ".cognistore-staging\U000e0100",
    ):
        for suffix in ("", "/nested"):
            with pytest.raises(ValueError, match="reserved namespace"):
                _invoke(driver, operation, alias + suffix, sentinel.name)
    assert sentinel.read_bytes() == b"private staging content"
    unchanged = sentinel.stat()
    assert (unchanged.st_ino, unchanged.st_size, unchanged.st_mtime_ns) == (
        original.st_ino, original.st_size, original.st_mtime_ns,
    )
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == [
        Path(".cognistore-staging"), Path(".cognistore-staging/upload-protected.tmp"),
    ]


@pytest.mark.parametrize("operation", OPERATIONS)
def test_posix_ignorable_empty_dot_and_dotdot_aliases_are_rejected_before_filesystem_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    driver = PosixDriver(str(tmp_path))

    def forbidden(*args, **kwargs):
        pytest.fail("ambiguous path component reached filesystem traversal")

    monkeypatch.setattr(driver, "_directory", forbidden)
    for ignored in ("\u00ad", "\u034f", "\u200c", "\ufe0f", "\ufeff", "\U000e0100"):
        for component in (ignored, f".{ignored}", f".{ignored}."):
            for path in (
                component, f"{component}/private", f"ordinary/{component}",
                f"ordinary/{component}/private",
            ):
                with pytest.raises(ValueError):
                    _invoke(driver, operation, path, "private.txt")
                # Raw POSIX listing prefixes are literal filters, not paths.
                if operation not in {"list_objects", "list_objects_page"}:
                    with pytest.raises(ValueError):
                        _invoke(driver, operation, "bucket", path)
    assert list(tmp_path.iterdir()) == []
