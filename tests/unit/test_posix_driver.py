import os
from pathlib import Path

import pytest

from cognistore.drivers.posix_driver import PosixDriver


def test_put_get_delete_roundtrip(tmp_path: Path):
	d = PosixDriver(base_path=str(tmp_path))
	bucket = "test"
	key = "a/b/c.txt"
	data = b"hello world"

	d.put_object(bucket, key, data)
	out = d.get_object(bucket, key)
	assert out == data

	st = d.stat_object(bucket, key)
	assert st["size"] == len(data)

	d.delete_object(bucket, key)
	with pytest.raises(FileNotFoundError):
		d.get_object(bucket, key)


def test_range_reads_and_writes(tmp_path: Path):
	d = PosixDriver(base_path=str(tmp_path))
	bucket = "bk"
	key = "file.bin"
	blob = b"0123456789"  # 10 bytes
	# Write bytes 3-6 (indexes inclusive) -> "3456"
	d.put_object(bucket, key, b"3456", range="bytes=3-6")
	# Fill beginning and end
	d.put_object(bucket, key, b"012", range="bytes=0-2")
	d.put_object(bucket, key, b"789", range="bytes=7-9")

	# Full read
	full = d.get_object(bucket, key)
	assert full == blob
	# Range read
	mid = d.get_object(bucket, key, range="bytes=2-5")
	assert mid == b"2345"


def test_list_objects_with_prefix(tmp_path: Path):
	d = PosixDriver(base_path=str(tmp_path))
	bucket = "buck"
	files = [
		("aa/one.txt", b"1"),
		("aa/two.txt", b"2"),
		("bb/three.txt", b"3"),
	]
	for k, v in files:
		d.put_object(bucket, k, v)

	all_keys = sorted(list(d.list_objects(bucket)))
	assert all_keys == ["aa/one.txt", "aa/two.txt", "bb/three.txt"]

	aa_keys = sorted(list(d.list_objects(bucket, prefix="aa/")))
	assert aa_keys == ["aa/one.txt", "aa/two.txt"]

