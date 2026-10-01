"""Create a deterministic evidence archive with an internal checksum inventory."""

import gzip
import hashlib
import io
import sys
import tarfile
from pathlib import Path

root = Path(sys.argv[1])
target = Path(sys.argv[2])
target.parent.mkdir(parents=True, exist_ok=True)
files = sorted(p for p in root.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
manifest = "".join(
    hashlib.sha256(p.read_bytes()).hexdigest() + "  " + str(p.relative_to(root)) + "\n"
    for p in files
).encode()
with (
    target.open("wb") as out,
    gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as zipped,
):
    with tarfile.open(fileobj=zipped, mode="w") as archive:
        for name, data in [("SHA256SUMS", manifest)] + [
            (str(p.relative_to(root)), p.read_bytes()) for p in files
        ]:
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            entry.mode = 0o644
            entry.mtime = 0
            archive.addfile(entry, io.BytesIO(data))
with tarfile.open(target, "r:gz") as archive:
    inventory = archive.extractfile("SHA256SUMS").read().decode().splitlines()
    for line in inventory:
        digest, name = line.split("  ", 1)
        assert hashlib.sha256(archive.extractfile(name).read()).hexdigest() == digest, name
print(
    target,
    len(files),
    "verified files",
    target.stat().st_size,
    "bytes",
    hashlib.sha256(target.read_bytes()).hexdigest(),
)
