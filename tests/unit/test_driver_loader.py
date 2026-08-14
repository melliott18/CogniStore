from pathlib import Path

import pytest

from cognistore.drivers import driver_loader
from cognistore.drivers.driver_loader import load_drivers


def _write_config(path: Path, hot_path: Path, warm_path: Path) -> None:
	path.write_text(
		"tiers:\n"
		"  hot:\n"
		"    driver: posix\n"
		f"    path: {hot_path}\n"
		"  warm:\n"
		"    driver: posix\n"
		f"    path: {warm_path}\n"
	)


class _ConstructedPosixDriver:
	def __init__(self, *, base_path: str, chunk_size: int) -> None:
		self.base = Path(base_path).resolve()
		self.chunk_size = chunk_size


def test_loader_passes_configured_posix_chunk_size(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	constructed: list[_ConstructedPosixDriver] = []

	def make_posix_driver(*, base_path: str, chunk_size: int) -> _ConstructedPosixDriver:
		driver = _ConstructedPosixDriver(
			base_path=base_path,
			chunk_size=chunk_size,
		)
		constructed.append(driver)
		return driver

	monkeypatch.setattr(driver_loader, "PosixDriver", make_posix_driver)
	config = tmp_path / "drivers.yaml"
	storage_path = tmp_path / "storage"
	config.write_text(
		"tiers:\n"
		"  hot:\n"
		"    driver: posix\n"
		f"    path: {storage_path}\n"
		"    chunk_size: 2097152\n"
	)

	loaded = driver_loader.load_drivers(str(config))

	assert loaded == {"hot": constructed[0]}
	assert constructed[0].base == storage_path.resolve()
	assert constructed[0].chunk_size == 2 * 1024 * 1024


def test_loader_rejects_duplicate_canonical_posix_roots(tmp_path: Path):
	root = tmp_path / "storage"
	config = tmp_path / "drivers.yaml"
	_write_config(config, root, root / "nested" / "..")

	with pytest.raises(ValueError, match="resolve to the same root"):
		load_drivers(str(config))

	assert not root.exists()


def test_loader_rejects_symlink_alias_roots(tmp_path: Path):
	root = tmp_path / "storage"
	alias = tmp_path / "storage-alias"
	root.mkdir()
	try:
		alias.symlink_to(root, target_is_directory=True)
	except (NotImplementedError, OSError):
		pytest.skip("directory symlinks are unavailable on this platform")
	config = tmp_path / "drivers.yaml"
	_write_config(config, root, alias)

	with pytest.raises(ValueError, match="resolve to the same root"):
		load_drivers(str(config))
