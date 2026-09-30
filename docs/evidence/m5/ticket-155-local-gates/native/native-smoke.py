import ctypes
import ctypes.util
import datetime
import json
import ssl
import subprocess
from zoneinfo import ZoneInfo

expected = {
    "openssl": "3.0.22-1~deb12u1",
    "libssl3:amd64": "3.0.22-1~deb12u1",
    "tzdata": "2026c-0+deb12u1",
}
rows = subprocess.check_output(
    ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\n", "openssl", "libssl3", "tzdata", "libmagic1"],
    text=True,
)
installed = dict(row.split("\t") for row in rows.splitlines())
for package, version in expected.items():
    assert installed[package] == version, (package, installed[package])

assert ssl.OPENSSL_VERSION.split()[1] == "3.0.22", ssl.OPENSSL_VERSION
context = ssl.create_default_context()
assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname

library_name = ctypes.util.find_library("magic")
assert library_name
magic = ctypes.CDLL(library_name)
magic.magic_open.argtypes = [ctypes.c_int]
magic.magic_open.restype = ctypes.c_void_p
magic.magic_load.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
magic.magic_load.restype = ctypes.c_int
magic.magic_buffer.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
magic.magic_buffer.restype = ctypes.c_char_p
magic.magic_close.argtypes = [ctypes.c_void_p]
handle = magic.magic_open(0x10)  # MAGIC_MIME_TYPE
assert handle
try:
    assert magic.magic_load(handle, None) == 0
    fixture = b"CogniStore local native smoke fixture.\n"
    mime_type = magic.magic_buffer(handle, fixture, len(fixture)).decode()
    assert mime_type == "text/plain", mime_type
finally:
    magic.magic_close(handle)

offset = datetime.datetime(2026, 9, 29, tzinfo=ZoneInfo("America/Los_Angeles")).utcoffset()
assert offset == datetime.timedelta(hours=-7), offset
print(json.dumps({
    "status": "passed",
    "scope": "disposable native stage only; offline synthetic fixture",
    "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "installed_packages": installed,
    "python_ssl": ssl.OPENSSL_VERSION,
    "tls_verification": "required with hostname checking",
    "libmagic": library_name,
    "fixture_mime_type": mime_type,
    "zoneinfo_fixture_offset_hours": offset.total_seconds() / 3600,
}, indent=2))
