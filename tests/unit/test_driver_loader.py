from pathlib import Path

import pytest

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
