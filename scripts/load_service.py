#!/usr/bin/env python3
"""HTTPS service adapter for explicitly authorized isolated load campaigns.

The journal is durable, sanitized evidence, not a credential store. Mutations are
submitted exactly once. An uncertain mutation stops admissions and is retained
for reconciliation; no automatic cleanup or replay is performed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import ssl
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from scripts.load_corpus import (
    DEFAULT_SEED,
    TENANTS,
    ObjectSpec,
    generate_payload,
)
from scripts.load_workload import Request, Result

JSON_LIMIT = 2 * 1024 * 1024
TIERS = ("hot", "warm")


class ServiceFailure(RuntimeError):
    """Only fixed public diagnostic codes are safe to expose."""


@dataclass(frozen=True)
class ServiceSettings:
    origin: str
    ca_file: Path
    token_files: dict[str, Path]
    bucket: str
    run_id: str
    seed: str = DEFAULT_SEED
    request_timeout: float = 25
    job_timeout: float = 300
    poll_interval: float = 1

    def __post_init__(self):
        origin = urlsplit(self.origin)
        if (origin.scheme != "https" or not origin.hostname or origin.username is not None
                or origin.password is not None or origin.path not in ("", "/")
                or origin.query or origin.fragment or any(c.isspace() for c in self.origin)
                or any(ord(c) < 32 or ord(c) == 127 for c in self.origin)):
            raise ValueError("invalid_https_origin")
        _ = origin.port
        if (set(self.token_files) != set(TENANTS)
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", self.bucket)
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", self.run_id)
                or not self.seed or len(self.seed.encode()) > 1024):
            raise ValueError("invalid_service_scope")
        for value, maximum in ((self.request_timeout, 30), (self.job_timeout, 86400),
                               (self.poll_interval, 30)):
            if type(value) not in (float, int) or not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError("invalid_service_timeout")


@dataclass
class Entry:
    spec: ObjectSpec
    key: str
    hotspot: bool = False
    churn: bool = False
    present: bool = False
    uncertain: bool = False
    tier: str = "hot"
    sha256: str | None = None
    initial: bool = True
    version: int = 0
    scanned: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LoadService:
    """Callable ``Request -> Result`` with bounded deterministic object pools.

    Base objects are all seeded. Up to 100 existing objects per tenant/size form
    a bounded churn pool alongside equally many initially absent slots. Full
    pilot corpora reserve 20 hot and 80 cold slots on each side, covering the
    deterministic request cycle plus in-flight headroom. Small local fixtures
    use five slots and make no sustained-workload capacity claim. The initial live
    count and MIME/size distribution exactly match the declared corpus. Twenty
    percent of both persistent and churn selection keys are hot.
    ``transport`` is an explicit test seam; the CLI never supplies it.
    """

    def __init__(self, settings: ServiceSettings, specs: Iterable[ObjectSpec],
                 journal_path: Path, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.prefix = f"load-qualification/{settings.run_id}/"
        self.stopped = False
        self.preflight_complete = False
        self.seed_complete = False
        self.unsettled_jobs = 0
        self._journal_path = journal_path
        self._transport = transport
        self._journal = None
        self._client = None
        self._job_slots = asyncio.Semaphore(2)
        self._audit_checkpoints: dict[str, dict] = {}
        self._hold_hashes: dict[str, str] = {}
        self._last_reconciliation_passed = False
        self._jobs: dict[str, dict] = {}
        self._scan_visits: dict[tuple[str, str], int] = {}
        self.entries: list[Entry] = []
        groups: dict[tuple[str, int], list[Entry]] = {}
        seen: set[tuple[str, str]] = set()
        ordinal: dict[tuple[str, int], int] = {}
        size_quota = {4096: 60, 65536: 30, 1048576: 9, 16777216: 1}
        for spec in specs:
            if (spec.tenant not in TENANTS or spec.size_bytes not in (4096, 65536, 1048576, 16777216)
                    or not re.fullmatch(r"[a-z0-9/_-]+", spec.key)
                    or (spec.tenant, spec.key) in seen):
                raise ValueError("invalid_corpus_spec")
            seen.add((spec.tenant, spec.key))
            identity = (spec.tenant, spec.size_bytes)
            index = ordinal.get(identity, 0)
            ordinal[identity] = index + 1
            group_index = index // size_quota[spec.size_bytes]
            # Every full scan group has the exact 60/30/9/1 size distribution.
            key = (f"{self.prefix}corpus/{group_index // 10:06d}/{group_index:06d}/"
                   f"{spec.size_bytes}-{index:08d}")
            entry = Entry(spec, key)
            self.entries.append(entry)
            groups.setdefault((spec.tenant, spec.size_bytes), []).append(entry)
        if not groups or set(tenant for tenant, _ in groups) != set(TENANTS):
            raise ValueError("both_tenant_corpora_required")
        for (tenant, size), group in groups.items():
            if len(group) < 5 or len(group) % 5:
                raise ValueError("exact_twenty_percent_hotspot_requires_multiple_of_five")
            for entry in group[:len(group) // 5]:
                entry.hotspot = True
            active_count = 100 if len(group) >= 100 else 5
            active_hot = active_count // 5
            active = (group[:active_hot]
                      + group[len(group) // 5:len(group) // 5 + 4 * active_hot])
            for number, original in enumerate(active):
                original.churn = True
                spec = ObjectSpec(tenant, f"churn/{size}/{number:02d}", size,
                                  original.spec.content_type)
                self.entries.append(Entry(spec, self.prefix + spec.key,
                                          hotspot=number < active_hot, churn=True, initial=False))
        self._by_identity = {(e.spec.tenant, e.key): e for e in self.entries}
        self._pools: dict[tuple[str, int, bool, bool], list[Entry]] = {}
        self._prefix_cache: dict[str, list[str]] = {}
        for entry in self.entries:
            identity = (entry.spec.tenant, entry.spec.size_bytes, entry.hotspot, entry.churn)
            self._pools.setdefault(identity, []).append(entry)

    @property
    def uncertain_count(self) -> int:
        return sum(entry.uncertain for entry in self.entries)

    async def __aenter__(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=str(self.settings.ca_file))
        self._journal = self._journal_path.open("x", encoding="utf-8")
        self._client = httpx.AsyncClient(
            base_url=self.settings.origin.rstrip("/"), verify=context,
            follow_redirects=False, trust_env=False, transport=self._transport,
            timeout=self.settings.request_timeout,
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=40),
        )
        self._emit("opened", base_objects=sum(e.initial for e in self.entries),
                   churn_slots=sum(e.churn for e in self.entries))
        return self

    async def __aexit__(self, *exc):
        try:
            if self._client:
                await self._client.aclose()
        finally:
            if self._journal:
                self._journal.close()

    def _emit(self, event: str, **values):
        if self._journal is None:
            raise ServiceFailure("journal_unavailable")
        row = {"event": event, "run_id": self.settings.run_id, "utc": datetime.now(timezone.utc).isoformat(), **values}
        self._journal.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        self._journal.flush()
        os.fsync(self._journal.fileno())

    def _token(self, tenant: str) -> str:
        if tenant not in TENANTS:
            raise ServiceFailure("unknown_tenant")
        try:
            with self.settings.token_files[tenant].open("rb") as source:
                data = source.read(32769)
            token = data.decode("ascii").strip()
            if len(data) > 32768 or not re.fullmatch(r"[A-Za-z0-9_.~+/-]+=*", token):
                raise ValueError()
            return token
        except (OSError, UnicodeError, ValueError):
            raise ServiceFailure("token_file_invalid") from None

    async def _request(self, *args, **kwargs):
        try:
            return await asyncio.wait_for(self._request_inner(*args, **kwargs),
                                          timeout=self.settings.request_timeout)
        except asyncio.TimeoutError:
            raise httpx.TimeoutException("request_deadline") from None

    async def _request_inner(self, tenant: str | None, method: str, path: str,
                       expected: tuple[int, ...] = (200,), *, max_bytes=JSON_LIMIT, **kwargs):
        headers = {"Accept-Encoding": "identity", **kwargs.pop("headers", {})}
        if tenant is not None:
            headers["Authorization"] = "Bearer " + self._token(tenant)
        async with self._client.stream(method, path, headers=headers, **kwargs) as response:
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise ServiceFailure("encoded_response")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > max_bytes:
                    raise ServiceFailure("response_too_large")
                content.extend(chunk)
            if response.status_code not in expected:
                raise ServiceFailure("http_status")
            return httpx.Response(response.status_code, headers=response.headers, content=bytes(content))

    def _json(self, response):
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise ServiceFailure("invalid_response_json") from None

    def object_path(self, entry: Entry, tier: str | None = None):
        return f"/v1/objects/{tier or entry.tier}/{self.settings.bucket}/{entry.key}"

    def catalog_path(self, entry: Entry):
        return f"/v1/catalog/objects/{self.settings.bucket}/{entry.key}"

    async def preflight(self):
        if self.preflight_complete:
            raise ServiceFailure("preflight_already_completed")
        if self._token(TENANTS[0]) == self._token(TENANTS[1]):
            raise ServiceFailure("distinct_tenant_tokens_required")
        for path in ("/healthz", "/readyz"):
            await self._request(None, "GET", path)
        await self._request(None, "GET", "/metrics", (404,))
        params = {"bucket": self.settings.bucket, "prefix": self.prefix, "limit": 1}
        await self._request(None, "GET", "/v1/catalog/objects", (401,), params=params)
        await self._request(None, "GET", "/v1/catalog/objects", (401,), params=params,
                            headers={"Authorization": "Bearer invalid-load-fixture-token"})
        for tenant in TENANTS:
            await self._request(tenant, "GET", "/metrics", (404,))
            page = self._json(await self._request(tenant, "GET", "/v1/catalog/objects", params=params))
            if page.get("items") != [] or page.get("page", {}).get("next_cursor"):
                raise ServiceFailure("run_prefix_not_empty")
            audit = self._json(await self._request(tenant, "POST", "/v1/audit/verify", json={}))
            if audit.get("valid") is not True or audit.get("tenant_id") != tenant:
                raise ServiceFailure("audit_baseline_invalid")
            self._audit_checkpoints[tenant] = audit["checkpoint"]
            holds = self._json(await self._request(tenant, "GET", "/v1/legal-holds",
                                                  params={"bucket": self.settings.bucket}))
            self._hold_hashes[tenant] = hashlib.sha256(json.dumps(holds, sort_keys=True).encode()).hexdigest()
            self._emit("audit_baseline", tenant=tenant, checkpoint=audit["checkpoint"],
                       holds_sha256=self._hold_hashes[tenant])
        self.preflight_complete = True
        self._emit("preflight_passed")

    async def _catalog(self, entry: Entry, *, require_scan: bool = False):
        row = self._json(await self._request(entry.spec.tenant, "GET", self.catalog_path(entry)))
        if (row.get("bucket") != self.settings.bucket or row.get("key") != entry.key
                or row.get("size") != entry.spec.size_bytes or row.get("tier") not in TIERS
                or row.get("metadata", {}).get("mime") != entry.spec.content_type):
            raise ServiceFailure("catalog_mismatch")
        checksum = row.get("metadata", {}).get("sha256")
        if checksum is not None and checksum != entry.sha256:
            raise ServiceFailure("catalog_checksum_mismatch")
        if require_scan or entry.scanned:
            metadata = row.get("metadata", {})
            # Native detector/extraction provenance is intentionally private in
            # this API. Retain only the public scan checksum and MIME contract;
            # private exporter evidence is required to attest extraction.
            if metadata.get("sha256") != entry.sha256:
                raise ServiceFailure("scan_metadata_mismatch")
            entry.scanned = True
        entry.tier = row["tier"]
        return row

    async def _put(self, entry: Entry, *, overwrite: bool):
        version = entry.version + int(overwrite)
        seed = self.settings.seed if version == 0 else f"{self.settings.seed}:version:{version}"
        payload = await asyncio.to_thread(generate_payload, entry.spec, seed)
        digest = hashlib.sha256(payload).hexdigest()
        self._last_reconciliation_passed = False
        entry.uncertain = True
        self._emit("mutation_intent", tenant=entry.spec.tenant, key=entry.key,
                   operation="replace" if overwrite else "create", sha256=digest, version=version)
        try:
            row = self._json(await self._request(
                entry.spec.tenant, "PUT", self.object_path(entry), (201,),
                params={"overwrite": str(overwrite).lower()}, content=payload,
                headers={"Content-Type": entry.spec.content_type},
            ))
            if (row.get("bucket") != self.settings.bucket or row.get("key") != entry.key
                    or row.get("size") != entry.spec.size_bytes or row.get("tier") != entry.tier
                    or not row.get("generation")):
                raise ServiceFailure("put_response_mismatch")
            entry.sha256 = digest
            entry.version = version
            entry.scanned = False
            entry.present = True
            entry.uncertain = False
            self._emit("mutation_completed", tenant=entry.spec.tenant, key=entry.key,
                       operation="put", sha256=digest)
        except BaseException:
            self.stopped = True
            self._emit("mutation_uncertain", tenant=entry.spec.tenant, key=entry.key, operation="put")
            raise

    async def seed(self):
        if not self.preflight_complete or self.seed_complete or self.stopped:
            raise ServiceFailure("seed_not_permitted")
        for entry in self.entries:
            if self.stopped:
                raise ServiceFailure("admissions_stopped")
            if not entry.initial:
                continue
            await self._put(entry, overwrite=False)
        self.seed_complete = True
        summary = self.inventory()
        self._emit("seed_completed", **summary)
        return summary

    def inventory(self):
        present = [entry for entry in self.entries if entry.present]
        return {"base_objects": sum(e.initial for e in present),
                "churn_objects": sum(e.churn for e in present),
                "total_objects": len(present),
                "scanned_objects": sum(e.scanned for e in present),
                "total_bytes": sum(e.spec.size_bytes for e in present),
                "uncertain_mutations": self.uncertain_count,
                "unsettled_jobs": self.unsettled_jobs}

    async def _select(self, request: Request) -> Entry:
        churn = request.operation == "delete" or request.put_kind == "create"
        pool = list(self._pools.get((request.tenant, request.size_bytes, request.hotspot, churn), []))
        if not churn:
            pool += self._pools.get((request.tenant, request.size_bytes, request.hotspot, True), [])
        required_present = request.put_kind != "create"
        eligible = [e for e in pool if e.present == required_present and not e.uncertain]
        if not eligible:
            raise ServiceFailure("matching_pool_exhausted")
        start = int(request.selection * len(eligible))
        if not 0 <= start < len(eligible):
            raise ServiceFailure("invalid_selection")
        # Reserve before state is inspected again; competing creates never overwrite.
        for entry in eligible[start:] + eligible[:start]:
            await entry.lock.acquire()
            if entry.present == required_present and not entry.uncertain:
                return entry
            entry.lock.release()
        raise ServiceFailure("matching_pool_exhausted")

    async def __call__(self, request: Request) -> Result:
        if not self.seed_complete or self.stopped:
            return Result(False, "adapter_error")
        entry = None
        try:
            entry = await self._select(request)
            if self.stopped:
                raise ServiceFailure("admissions_stopped")
            if request.operation == "get":
                await self._verify_get(entry)
            elif request.operation == "head":
                await self._verify_head(entry)
            elif request.operation == "catalog":
                await self._catalog(entry)
            elif request.operation == "ask":
                row = self._json(await self._request(request.tenant, "POST", "/v1/ask", json={
                    "text": entry.key, "retrieval_mode": "metadata",
                    "synthesize": False, "limit": 1,
                    "filters": {"bucket": self.settings.bucket, "key_prefix": entry.key},
                }))
                results = row.get("results", [])
                if (row.get("mode") != "metadata" or row.get("generation_status") != "not_requested"
                        or row.get("answer") is not None or len(results) != 1
                        or results[0].get("citation", {}).get("key") != entry.key
                        or results[0].get("citation", {}).get("bucket") != self.settings.bucket):
                    raise ServiceFailure("ask_mismatch")
            elif request.operation == "policy_preview":
                row = self._json(await self._request(request.tenant, "POST", "/v1/policies/preview",
                    json={"bucket": self.settings.bucket, "key": entry.key,
                          "config": {"policy": "simple", "allowed_tiers": list(TIERS)}}))
                if (row.get("bucket") != self.settings.bucket or row.get("key") != entry.key
                        or row.get("current", {}).get("tier") != entry.tier
                        or row.get("execution", {}).get("mode") != "preview"
                        or row.get("execution", {}).get("state") != "dry_run"):
                    raise ServiceFailure("preview_mismatch")
            elif request.operation == "put" and request.put_kind in ("create", "replace"):
                await self._put(entry, overwrite=request.put_kind == "replace")
            elif request.operation == "delete":
                await self._delete(entry)
            else:
                raise ServiceFailure("unknown_operation")
            return Result(True, "ok")
        except httpx.TimeoutException:
            return Result(False, "timeout")
        except httpx.TransportError:
            return Result(False, "transport_error")
        except ServiceFailure as error:
            return Result(False, "http_error" if str(error) == "http_status" else "adapter_error")
        finally:
            if entry is not None:
                entry.lock.release()

    async def _verify_head(self, entry):
        response = await self._request(entry.spec.tenant, "HEAD", self.object_path(entry), max_bytes=0)
        if (response.headers.get("Content-Length") != str(entry.spec.size_bytes)
                or not response.headers.get("ETag")):
            raise ServiceFailure("head_mismatch")

    async def _verify_get(self, entry):
        response = await self._request(entry.spec.tenant, "GET", self.object_path(entry),
                                       max_bytes=entry.spec.size_bytes)
        if (len(response.content) != entry.spec.size_bytes
                or hashlib.sha256(response.content).hexdigest() != entry.sha256):
            raise ServiceFailure("get_checksum_mismatch")

    async def _delete(self, entry):
        self._last_reconciliation_passed = False
        entry.uncertain = True
        self._emit("mutation_intent", tenant=entry.spec.tenant, key=entry.key, operation="delete")
        try:
            await self._request(entry.spec.tenant, "DELETE", self.object_path(entry), (204,), max_bytes=0)
            entry.present = False
            entry.uncertain = False
            self._emit("mutation_completed", tenant=entry.spec.tenant, key=entry.key, operation="delete")
        except BaseException:
            self.stopped = True
            self._emit("mutation_uncertain", tenant=entry.spec.tenant, key=entry.key, operation="delete")
            raise

    def job_prefixes(self, kind: str) -> list[str]:
        if kind not in ("catalog.scan", "policy.run"):
            raise ValueError("invalid_job_kind")
        depth = 2 if kind == "catalog.scan" else 1
        if kind in self._prefix_cache:
            return self._prefix_cache[kind]
        prefixes = sorted({self.prefix + "corpus/" + "/".join(e.key[len(self.prefix + 'corpus/'):].split('/')[:depth]) + "/"
                       for e in self.entries if e.initial}
                       | {entry.key.rsplit("/", 1)[0] + "/" for entry in self.entries if not entry.initial},
                       key=lambda prefix: (prefix.startswith(self.prefix + "churn/"), prefix))
        self._prefix_cache[kind] = prefixes
        return prefixes

    async def run_job(self, tenant: str, kind: str, prefix: str, config: dict | None = None,
                      *, tier: str | None = "hot"):
        if (not self.seed_complete or self.stopped or tenant not in TENANTS
                or prefix not in (self.job_prefixes(kind) + self.job_prefixes("catalog.scan"))
                or tier is not None and tier not in TIERS):
            raise ServiceFailure("job_scope_not_permitted")
        selected = [e for e in self.entries if e.spec.tenant == tenant and e.key.startswith(prefix)]
        limit = 100 if kind == "catalog.scan" else 1000
        if not 0 < len(selected) <= limit:
            raise ServiceFailure("job_scope_bound")
        body = {"bucket": self.settings.bucket, "prefix": prefix}
        if kind == "catalog.scan":
            path = "/v1/actions/catalog-scans"
        else:
            body["config"] = config or {"policy": "simple", "threshold": 1, "allowed_tiers": list(TIERS)}
            path = "/v1/actions/policy-runs"
        async with self._job_slots, AsyncExitStack() as locks:
            for entry in selected:
                await locks.enter_async_context(entry.lock)
            if self.stopped:
                raise ServiceFailure("admissions_stopped")
            verification_entries = [entry for entry in selected if entry.present]
            object_count = len(selected)
            if kind == "catalog.scan":
                # Foreground mutations and other jobs cannot change this owned
                # scope between selection, submission and verification.
                present = {candidate: [entry for entry in verification_entries if entry.tier == candidate]
                           for candidate in TIERS}
                available = [candidate for candidate in TIERS if present[candidate]]
                if not available or tier is not None and not present[tier]:
                    raise ServiceFailure("scan_scope_empty")
                visit_key = (tenant, prefix)
                if tier is None:
                    tier = available[self._scan_visits.get(visit_key, 0) % len(available)]
                verification_entries = present[tier]
                object_count = len(verification_entries)
                body["tier"] = tier
                self._scan_visits[visit_key] = self._scan_visits.get(visit_key, 0) + 1
            self._last_reconciliation_passed = False
            self.unsettled_jobs += 1
            self._emit("job_intent", tenant=tenant, kind=kind, prefix=prefix, tier=tier)
            async def execute():
                response = await self._request(tenant, "POST", path, (202,), json=body)
                row = self._json(response)
                try:
                    job_id = str(UUID(row["job_id"]))
                except (KeyError, ValueError, TypeError):
                    raise ServiceFailure("job_id_invalid") from None
                status_path = f"/v1/jobs/{job_id}"
                if (row.get("job_type") != kind or row.get("status_url") != status_path
                        or response.headers.get("Location") != status_path):
                    raise ServiceFailure("job_contract_invalid")
                self._jobs[job_id] = {"tenant": tenant, "kind": kind,
                                      "object_count": object_count, "job_id": job_id}
                self._emit("job_accepted", tenant=tenant, kind=kind, job_id=job_id,
                           object_count=object_count, prefix=prefix)
                while True:
                    row = self._json(await self._request(tenant, "GET", status_path))
                    state = row.get("status")
                    if (row.get("job_id") != job_id or row.get("job_type") != kind
                            or state not in ("queued", "running", "retrying", "succeeded", "failed")):
                        raise ServiceFailure("job_status_invalid")
                    self._emit("job_poll", tenant=tenant, kind=kind, job_id=job_id, status=state)
                    if state in ("succeeded", "failed"):
                        self.unsettled_jobs -= 1
                        if state != "succeeded":
                            raise ServiceFailure("job_failed")
                        for entry in verification_entries:
                            await self._catalog(entry, require_scan=kind == "catalog.scan")
                            await self._verify_get(entry)
                        return {"tenant": tenant, "kind": kind, "job_id": job_id,
                                "status": state, "verified_objects": len(verification_entries)}
                    await asyncio.sleep(self.settings.poll_interval)
            try:
                return await asyncio.wait_for(execute(), timeout=self.settings.job_timeout)
            except BaseException:
                self.stopped = True
                self._emit("job_requires_reconciliation", tenant=tenant, kind=kind, prefix=prefix)
                raise

    @staticmethod
    async def _gather(*coroutines):
        tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def policy_prefixes(self):
        mutable = [entry.key for entry in self.entries if entry.churn]
        prefixes = [prefix for prefix in self.job_prefixes("policy.run")
                    if prefix.startswith(self.prefix + "corpus/")
                    and not any(key.startswith(prefix) for key in mutable)]
        # A reduced local corpus may have no complete immutable 1000-key group.
        return prefixes or self.movement_prefixes()

    async def background_once(self, kind: str, cycle: int, config: dict | None = None):
        prefixes = self.policy_prefixes() if kind == "policy.run" else self.job_prefixes(kind)
        if not prefixes:
            raise ServiceFailure("no_immutable_policy_groups")
        if kind != "catalog.scan":
            prefix = prefixes[cycle % len(prefixes)]
            return await self._gather(*(self.run_job(tenant, kind, prefix, config) for tenant in TENANTS))

        async def scan(tenant):
            # Churn can empty a prefix. Examine owned state under the same
            # locks used by run_job before submitting; empty scans are never
            # submitted or counted as successful indexing attempts.
            for offset in range(len(prefixes)):
                prefix = prefixes[(cycle + offset) % len(prefixes)]
                try:
                    return await self.run_job(tenant, kind, prefix, tier=None)
                except ServiceFailure as error:
                    if str(error) != "scan_scope_empty":
                        raise
                    self._emit("scan_empty_prefix_skipped", tenant=tenant, prefix=prefix)
            raise ServiceFailure("no_nonempty_scan_scope")

        return await self._gather(*(scan(tenant) for tenant in TENANTS))

    async def reconcile(self):
        """Read every retained object and complete catalog; never erase uncertainty.

        Caller must drain foreground/background tasks first. An interrupted write
        remains an explicit failed gate even if the observed payload is intact.
        """
        mismatches = 0
        actual_count = 0
        digest = hashlib.sha256()
        placements = {tier: 0 for tier in TIERS}
        for tenant in TENANTS:
            seen: set[str] = set()
            cursor = None
            cursors: set[str] = set()
            while True:
                params = {"bucket": self.settings.bucket, "prefix": self.prefix, "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                page = self._json(await self._request(tenant, "GET", "/v1/catalog/objects", params=params))
                rows = page.get("items")
                if not isinstance(rows, list) or len(rows) > 200:
                    raise ServiceFailure("catalog_page_invalid")
                for row in rows:
                    key = row.get("key")
                    entry = self._by_identity.get((tenant, key))
                    if entry is None or key in seen or not entry.present:
                        mismatches += 1
                    seen.add(key)
                cursor = page.get("page", {}).get("next_cursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or len(cursor) > 16384 or cursor in cursors:
                    raise ServiceFailure("catalog_cursor_invalid")
                cursors.add(cursor)
                if len(cursors) > len(self.entries) + 1:
                    raise ServiceFailure("catalog_page_bound")
            for entry in self.entries:
                if entry.spec.tenant != tenant:
                    continue
                async with entry.lock:
                    if entry.present:
                        if entry.key not in seen:
                            mismatches += 1
                            continue
                        await self._catalog(entry)
                        await self._verify_head(entry)
                        await self._verify_get(entry)
                        actual_count += 1
                        placements[entry.tier] += 1
                        digest.update(json.dumps([tenant, entry.key, entry.sha256, entry.tier]).encode())
                        for tier in TIERS:
                            if tier != entry.tier:
                                await self._request(tenant, "HEAD", self.object_path(entry, tier), (404,))
                    elif not entry.uncertain:
                        await self._request(tenant, "GET", self.catalog_path(entry), (404,))
                        for tier in TIERS:
                            await self._request(tenant, "HEAD", self.object_path(entry, tier), (404,))
        snapshots = {}
        for tenant in TENANTS:
            holds = self._json(await self._request(tenant, "GET", "/v1/legal-holds",
                                                   params={"bucket": self.settings.bucket}))
            audit = self._json(await self._request(tenant, "POST", "/v1/audit/verify", json={"checkpoint": self._audit_checkpoints[tenant]}))
            if audit.get("valid") is not True or audit.get("tenant_id") != tenant:
                mismatches += 1
            hold_hash = hashlib.sha256(json.dumps(holds, sort_keys=True).encode()).hexdigest()
            if hold_hash != self._hold_hashes.get(tenant):
                mismatches += 1
            snapshots[tenant] = {
                "holds_sha256": hold_hash,
                "audit_sha256": hashlib.sha256(json.dumps(audit, sort_keys=True).encode()).hexdigest(),
                "audit_valid": audit.get("valid") is True,
            }
        result = {"status": "passed" if not (mismatches or self.uncertain_count or self.unsettled_jobs) else "failed",
                  "catalog_mismatches": mismatches, "verified_objects": actual_count,
                  "manifest_sha256": digest.hexdigest(), "placements": placements,
                  "snapshots": snapshots, **self.inventory()}
        self._last_reconciliation_passed = result["status"] == "passed"
        self._emit("reconciliation", **result)
        return result

    async def probe(self):
        for path in ("/healthz", "/readyz"):
            await self._request(None, "GET", path)
        for tenant in TENANTS:
            await self._request(tenant, "GET", "/metrics", (404,))
        return {"status": "passed", "uncertain_mutations": self.uncertain_count,
                "unsettled_jobs": self.unsettled_jobs}

    def movement_prefixes(self):
        """Immutable groups keep the exact size mix while the churn pool changes."""
        blocked = {entry.key.rsplit("/", 1)[0] + "/" for entry in self.entries
                   if entry.initial and entry.churn}
        return [prefix for prefix in self.job_prefixes("catalog.scan")
                if prefix.startswith(self.prefix + "corpus/") and prefix not in blocked]

    async def move_batch(self, direction: str, batch_index: int):
        if direction not in ("hot-to-warm", "warm-to-hot") or type(batch_index) is not int or batch_index < 0:
            raise ValueError("invalid_movement_batch")
        destination = "warm" if direction == "hot-to-warm" else "hot"
        prefixes = self.movement_prefixes()
        if not prefixes:
            raise ServiceFailure("no_immutable_movement_groups")
        prefix = prefixes[batch_index % len(prefixes)]
        config = {"policy": "simple", "threshold": 1 if destination == "warm" else 16777217,
                  "allowed_tiers": list(TIERS)}
        candidates = [entry for entry in self.entries if entry.present and entry.key.startswith(prefix)]
        before = {id(entry): entry.tier for entry in candidates}
        results = await self._gather(*(self.run_job(tenant, "policy.run", prefix, config)
                                         for tenant in TENANTS))
        matching = [entry for entry in self.entries if entry.present and entry.key.startswith(prefix)]
        if any(entry.tier != destination for entry in matching):
            self.stopped = True
            raise ServiceFailure("movement_placement_mismatch")
        result = {"direction": direction, "batch_index": batch_index,
                  "verified_objects": len(matching),
                  "verified_moves": sum(before[id(entry)] != destination for entry in matching),
                  "size_histogram": {str(size): sum(entry.spec.size_bytes == size for entry in matching)
                                     for size in (4096, 65536, 1048576, 16777216)},
                  "jobs": results}
        self._emit("movement_batch_verified", **result)
        return result

    async def cleanup(self):
        """Delete only owned acknowledged objects after a clean, drained reconciliation."""
        if (self.stopped or self.uncertain_count or self.unsettled_jobs
                or not self._last_reconciliation_passed):
            raise ServiceFailure("cleanup_not_permitted")
        removed = 0
        for entry in self.entries:
            async with entry.lock:
                if self.stopped:
                    raise ServiceFailure("admissions_stopped")
                if entry.present:
                    await self._catalog(entry)
                    await self._delete(entry)
                    removed += 1
        report = await self.reconcile()
        if report["status"] != "passed" or report["total_objects"]:
            raise ServiceFailure("cleanup_reconciliation_failed")
        self._emit("cleanup_completed", removed_objects=removed)
        return {"status": "passed", "removed_objects": removed}

    async def move_all(self, direction: str):
        """Bounded two-job batches covering every owned prefix, including churn."""
        if direction not in ("hot-to-warm", "warm-to-hot"):
            raise ValueError("invalid_movement_direction")
        destination = "warm" if direction == "hot-to-warm" else "hot"
        config = {"policy": "simple", "threshold": 1 if destination == "warm" else 16777217,
                  "allowed_tiers": list(TIERS)}
        verified_objects = 0
        verified_moves = 0
        for prefix in self.job_prefixes("policy.run"):
            if self.stopped:
                raise ServiceFailure("admissions_stopped")
            entries = [entry for entry in self.entries if entry.present and entry.key.startswith(prefix)]
            before = {id(entry): entry.tier for entry in entries}
            await self._gather(*(self.run_job(tenant, "policy.run", prefix, config) for tenant in TENANTS))
            if any(entry.tier != destination for entry in entries if entry.present):
                self.stopped = True
                raise ServiceFailure("movement_placement_mismatch")
            verified_objects += sum(entry.present for entry in entries)
            verified_moves += sum(entry.present and before[id(entry)] != destination for entry in entries)
        result = {"status": "passed", "direction": direction, "verified_objects": verified_objects,
                  "verified_moves": verified_moves}
        self._emit("movement_all_verified", **result)
        return result

    @staticmethod
    def _audit_time(value):
        if not isinstance(value, str) or len(value) > 64:
            raise ServiceFailure("invalid_audit_timestamp")
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise ValueError()
            return instant.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            raise ServiceFailure("invalid_audit_timestamp") from None

    async def export_job_observations(self):
        """Normalize retained worker lifecycle evidence for this adapter's jobs.

        ``accepted_at`` is the first *worker* acceptance event, not an HTTP poll
        or response time. For scans, ``object_count`` is the actual nonempty
        tier cohort selected and verified under scope locks; for policy jobs
        it is the owned scope bound, not an invented scanner-returned count. Missing native
        publication time, attempts or terminals produce gaps, never successes.
        """
        output = {"jobs": [], "scans": [], "gaps": []}
        lifecycle = ("job.started", "job.succeeded", "job.failure", "job.dead_lettered")
        for job_id, owned in self._jobs.items():
            events = []
            seen_ids = set()
            try:
                for event_type in lifecycle:
                    cursor = None
                    cursors = set()
                    for _ in range(100):
                        params = {"job_id": job_id, "event_type": event_type, "limit": 200}
                        if cursor:
                            params["cursor"] = cursor
                        page = self._json(await self._request(owned["tenant"], "GET", "/v1/audit/events",
                                                              params=params))
                        items = page.get("items")
                        if not isinstance(items, list) or len(items) > 200:
                            raise ServiceFailure("audit_page_invalid")
                        for row in items:
                            details = row.get("details", {})
                            event_id = str(UUID(row["event_id"]))
                            attempt = details.get("cumulative_attempt")
                            if (event_id in seen_ids or row.get("job_id") != job_id
                                    or row.get("event_type") != event_type
                                    or details.get("job_type") != owned["kind"]
                                    or type(attempt) is not int or attempt < 1):
                                raise ServiceFailure("audit_event_contract_invalid")
                            seen_ids.add(event_id)
                            occurred = self._audit_time(row.get("occurred_at")).isoformat()
                            published = self._audit_time(details.get("source_published_at")).isoformat()
                            events.append({"event_id": event_id, "event_type": event_type,
                                           "job_id": job_id, "attempt_number": attempt,
                                           "occurred_at": occurred, "source_published_at": published})
                        cursor = page.get("page", {}).get("next_cursor")
                        if cursor is None:
                            break
                        if not isinstance(cursor, str) or len(cursor) > 16384 or cursor in cursors:
                            raise ServiceFailure("audit_cursor_invalid")
                        cursors.add(cursor)
                    else:
                        raise ServiceFailure("audit_page_bound")
                if not events:
                    raise ServiceFailure("worker_audit_missing")
                by_attempt: dict[int, list[dict]] = {}
                for row in events:
                    by_attempt.setdefault(row["attempt_number"], []).append(row)
                if sorted(by_attempt) != list(range(1, max(by_attempt) + 1)):
                    raise ServiceFailure("worker_attempt_gap")
                published = min(self._audit_time(row["source_published_at"]) for row in events)
                attempts = []
                accepted = None
                prior_completed = published
                for number, rows in sorted(by_attempt.items()):
                    starts = [row for row in rows if row["event_type"] == "job.started"]
                    terminals = [row for row in rows if row["event_type"] != "job.started"]
                    if len(starts) != 1 or not terminals:
                        raise ServiceFailure("worker_attempt_unsettled")
                    # A failure can be followed by its terminal DLQ decision;
                    # preserve the later durable terminal, never double-count.
                    terminals.sort(key=lambda row: self._audit_time(row["occurred_at"]))
                    terminal = terminals[-1]
                    if len(terminals) > 1 and {row["event_type"] for row in terminals} != {
                            "job.failure", "job.dead_lettered"}:
                        raise ServiceFailure("worker_terminal_ambiguous")
                    began = self._audit_time(starts[0]["occurred_at"])
                    completed = self._audit_time(terminal["occurred_at"])
                    if not prior_completed <= began <= completed:
                        raise ServiceFailure("worker_chronology_invalid")
                    accepted = accepted or began
                    success = terminal["event_type"] == "job.succeeded"
                    if number != max(by_attempt) and success:
                        raise ServiceFailure("worker_replay_requires_review")
                    prior_completed = completed
                    attempts.append({"attempt_id": terminal["event_id"], "job_id": job_id,
                                     "attempt_number": number, "published_at": published.isoformat(),
                                     "completed_at": completed.isoformat(), "success": success})
                # A retryable failure is not a reconciled job terminal.
                final_events = by_attempt[max(by_attempt)]
                if not any(row["event_type"] in ("job.succeeded", "job.dead_lettered") for row in final_events):
                    raise ServiceFailure("worker_job_unsettled")
                job = {**owned, "kind": "scan" if owned["kind"] == "catalog.scan" else "policy",
                       "published_at": published.isoformat(), "accepted_at": accepted.isoformat(),
                       "completed_at": attempts[-1]["completed_at"], "attempt_count": len(attempts),
                       "outcome": "succeeded" if attempts[-1]["success"] else "failed"}
                output["jobs"].append(job)
                if owned["kind"] == "catalog.scan":
                    output["scans"].extend(attempts)
                self._emit("worker_audit_export", tenant=owned["tenant"], job_id=job_id,
                           records=events,
                           records_sha256=hashlib.sha256(json.dumps(events, sort_keys=True).encode()).hexdigest())
            except (ServiceFailure, ValueError, TypeError, KeyError, httpx.HTTPError) as error:
                reason = str(error) if isinstance(error, ServiceFailure) else "worker_audit_unavailable"
                gap = {"job_id": job_id, "tenant": owned["tenant"], "reason": reason}
                output["gaps"].append(gap)
                self._emit("worker_audit_gap", **gap)
        return output

    async def normalize_churn(self):
        """Restore the declared live corpus between drained measurement phases."""
        if not self.seed_complete or self.stopped or self.uncertain_count or self.unsettled_jobs:
            raise ServiceFailure("churn_normalization_not_permitted")
        # Remove acknowledged extra keys before filling holes in original keys.
        # Unknown writes are never treated as absent and never replayed.
        for initial in (False, True):
            for entry in self.entries:
                if not entry.churn or entry.initial != initial:
                    continue
                async with entry.lock:
                    if self.stopped:
                        raise ServiceFailure("admissions_stopped")
                    if not initial and entry.present:
                        await self._delete(entry)
                    elif initial and not entry.present:
                        await self._put(entry, overwrite=False)
        result = self.inventory()
        if result["total_objects"] != sum(entry.initial for entry in self.entries):
            self.stopped = True
            raise ServiceFailure("churn_normalization_mismatch")
        self._emit("churn_normalized", **result)
        return result
