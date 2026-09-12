"""Small stateful GCS JSON/HTTP emulator for deterministic protocol faults.

Integration tests also use fake-gcs-server; this in-process emulator can model
lost acknowledgements, partial acceptance, and publication races precisely.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote

import httpx


class GCSFake:
    endpoint = "http://gcs.test"

    def __init__(self) -> None:
        self.buckets: set[str] = {"bucket"}
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.generation = 0
        self.session_counter = 0
        self.hook: Callable[[httpx.Request], httpx.Response | None] | None = None

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def store(
        self, bucket: str, key: str, payload: bytes, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.generation += 1
        self.buckets.add(bucket)
        result = {
            **(metadata or {}),
            "name": key,
            "bucket": bucket,
            "size": str(len(payload)),
            "generation": str(self.generation),
            "updated": "2025-01-02T03:04:05.000Z",
        }
        self.objects[bucket, key] = {"resource": result, "payload": payload}
        return result

    @staticmethod
    def error(status: int) -> httpx.Response:
        # Deliberately include a secret in provider text: callers must sanitize it.
        return httpx.Response(status, json={"error": {"message": "provider-secret"}})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.hook:
            response = self.hook(request)
            if response is not None:
                return response
        return self.respond(request)

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.raw_path.decode().split("?", 1)[0])
        params = request.url.params
        if path.startswith("/session/"):
            return self._upload(request, path)
        if path == "/storage/v1/b" and request.method == "POST":
            bucket = json.loads(request.content)["name"]
            if bucket in self.buckets:
                return self.error(409)
            self.buckets.add(bucket)
            return httpx.Response(200, json={"name": bucket})

        match = re.fullmatch(r"/(upload/)?storage/v1/b/([^/]+)(?:/o(?:/(.*))?)?", path)
        assert match, f"Unexpected GCS request path: {path}"
        upload, bucket, key = match.groups()
        if bucket not in self.buckets:
            return self.error(404)
        if upload and request.method == "POST":
            assert params["uploadType"] == "resumable"
            metadata = json.loads(request.content)
            key = metadata["name"]
            if params.get("ifGenerationMatch") == "0" and (bucket, key) in self.objects:
                return self.error(412)
            self.session_counter += 1
            session = f"/session/{self.session_counter}"
            self.sessions[session] = {
                "bucket": bucket,
                "key": key,
                "metadata": metadata,
                "total": int(request.headers["X-Upload-Content-Length"]),
                "exclusive": params.get("ifGenerationMatch") == "0",
                "data": bytearray(),
            }
            return httpx.Response(200, headers={"Location": self.endpoint + session})

        if not path.endswith("/o") and key is None:
            return httpx.Response(200, json={"name": bucket})
        if key is None:
            keys = sorted(
                k for b, k in self.objects
                if b == bucket and k.startswith(params.get("prefix", ""))
            )
            start = int(params.get("pageToken", "0"))
            limit = int(params.get("maxResults", "1000"))
            page: dict[str, Any] = {"items": [{"name": k} for k in keys[start:start + limit]]}
            if start + limit < len(keys):
                page["nextPageToken"] = str(start + limit)
            return httpx.Response(200, json=page)

        obj = self.objects.get((bucket, key))
        if obj is None:
            return self.error(404)
        generation = params.get("ifGenerationMatch")
        if generation is not None and generation != obj["resource"]["generation"]:
            return self.error(412)
        if request.method == "DELETE":
            del self.objects[bucket, key]
            return httpx.Response(204)
        if params.get("alt") != "media":
            return httpx.Response(200, json=obj["resource"])
        payload = obj["payload"]
        status = 200
        headers: dict[str, str] = {}
        if "range" in request.headers:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers["range"])
            assert match
            first, last = match.groups()
            total = len(payload)
            start = int(first) if first else max(0, total - int(last))
            end = min(int(last), total - 1) if first and last else total - 1
            payload = payload[start:end + 1]
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            status = 206
        headers["Content-Length"] = str(len(payload))
        # Snapshot at GET time, including generation checks, before yielding data.
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(payload))

    def _upload(self, request: httpx.Request, path: str) -> httpx.Response:
        session = self.sessions.get(path)
        if session is None:
            return self.error(404)
        if request.method == "DELETE":
            del self.sessions[path]
            return httpx.Response(499)
        assert request.method == "PUT"
        if "committed" in session:
            return httpx.Response(200, json=session["committed"])
        content_range = request.headers["Content-Range"]
        total = session["total"]
        if content_range.startswith("bytes */"):
            assert not request.content
            if total != 0:
                return self.progress(path)
        else:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
            assert match, content_range
            start, end, supplied_total = map(int, match.groups())
            assert supplied_total == total
            assert len(request.content) == end - start + 1
            assert start == len(session["data"]), "resumption must use the acknowledged offset"
            session["data"].extend(request.content)
            if len(session["data"]) < total:
                assert len(request.content) % (256 * 1024) == 0
                return self.progress(path)
        assert len(session["data"]) == total
        expected_md5 = session["metadata"].get("md5Hash")
        if expected_md5:
            digest = hashlib.md5(session["data"], usedforsecurity=False).digest()
            if base64.b64encode(digest).decode() != expected_md5:
                return self.error(400)
        bucket, key = session["bucket"], session["key"]
        if session["exclusive"] and (bucket, key) in self.objects:
            return self.error(412)
        resource = self.store(bucket, key, bytes(session["data"]), session["metadata"])
        session["committed"] = resource
        return httpx.Response(200, json=resource)

    def progress(self, path: str) -> httpx.Response:
        session = self.sessions[path]
        if "committed" in session:
            return httpx.Response(200, json=session["committed"])
        length = len(session["data"])
        headers = {"Range": f"bytes=0-{length - 1}"} if length else {}
        return httpx.Response(308, headers=headers)
