"""Service-only gateway used by the versioned HTTP transport."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Generator, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, cast
from uuid import UUID

from cognistore.core.audit import (
    AuditContext,
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    AuditQuery,
    stable_audit_event_id,
)
from cognistore.core.catalog import CatalogStore, ObjectRecord
from cognistore.core.policy_factory import build_policy
from cognistore.drivers.storage_driver import DEFAULT_STREAM_CHUNK_SIZE, StorageDriver
from cognistore.jobs.handlers import CATALOG_SCAN_JOB, POLICY_RUN_JOB, policy_job_payload
from cognistore.jobs.models import STATUS_TRACKING_METADATA, JobEnvelope
from cognistore.jobs.protocols import JobQueue
from cognistore.search import AskFilters, AskQuery, AskService

from .errors import (
    BackendUnavailableError,
    RangeNotSatisfiableError,
    RequestContractError,
    ResourceNotFoundError,
)
from .models import (
    AskRequest,
    AskResponse,
    CatalogObject,
    CatalogObjectPage,
    CatalogScanRequest,
    GeneratedAnswerResponse,
    JobState,
    JobStatusResponse,
    JSONValue,
    ObjectCitationResponse,
    ObjectResource,
    PageMetadata,
    PassageCitationResponse,
    PassageMatchResponse,
    PassageSignalValue,
    PolicyConfig,
    PolicyEvaluationRequest,
    PolicyEvaluationResponse,
    PolicyRunRequest,
    ProviderDiagnosticResponse,
    RetrievalModeValue,
    RetrievalResultResponse,
    RetrievalSignalValue,
    RetrievedPassageResponse,
    ScoreComponentResponse,
)
from .pagination import CursorError, decode_cursor, encode_cursor

_CATALOG_CURSOR_RESOURCE = "catalog.objects"
_BYTE_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$", re.ASCII)
_MAX_PUBLIC_METADATA_BYTES = 16 * 1024
_INTERNAL_OBJECT_METADATA_KEYS = frozenset(
    {
        "content_identity",
        "document_extraction",
        "etag",
        "mime_detection",
        "mtime",
        "path",
        "sample_len",
        "version_id",
    }
)
_STATUS_EVENT_TYPES = frozenset(
    {
        AuditEventType.JOB_QUEUED.value,
        AuditEventType.JOB_STARTED.value,
        AuditEventType.JOB_SUCCEEDED.value,
        AuditEventType.JOB_SUBMISSION_FAILED.value,
        AuditEventType.JOB_FAILURE.value,
        AuditEventType.JOB_RETRY.value,
        AuditEventType.JOB_DEAD_LETTERED.value,
    }
)


@dataclass(frozen=True)
class ObjectDownload:
    resource: ObjectResource
    chunks: Iterator[bytes]
    content_type: str
    content_length: int
    content_range: str | None = None
    close: Callable[[], None] = lambda: None


class APIGateway(Protocol):
    async def startup(self) -> None: ...

    async def shutdown(self) -> None: ...

    def put_object(
        self,
        tier: str,
        bucket: str,
        key: str,
        data: bytes,
        *,
        overwrite: bool,
        content_type: str | None,
    ) -> ObjectResource: ...

    def open_object(
        self,
        tier: str,
        bucket: str,
        key: str,
        *,
        byte_range: str | None,
    ) -> ObjectDownload: ...

    def stat_object(self, tier: str, bucket: str, key: str) -> ObjectResource: ...

    def delete_object(self, tier: str, bucket: str, key: str) -> None: ...

    def get_catalog_object(self, bucket: str, key: str) -> CatalogObject: ...

    def list_catalog_objects(
        self,
        *,
        bucket: str,
        prefix: str,
        tier: str | None,
        limit: int,
        cursor: str | None,
    ) -> CatalogObjectPage: ...

    def ask(self, request: AskRequest) -> AskResponse: ...

    def evaluate_policy(
        self, request: PolicyEvaluationRequest
    ) -> PolicyEvaluationResponse: ...

    async def submit_catalog_scan(
        self, request: CatalogScanRequest
    ) -> JobStatusResponse: ...

    async def submit_policy_run(
        self, request: PolicyRunRequest
    ) -> JobStatusResponse: ...

    def get_job(self, job_id: str) -> JobStatusResponse: ...


class UnavailableGateway:
    """Schema-generation default that never reaches infrastructure."""

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def __getattr__(self, _name: str):
        def unavailable(*_args, **_kwargs):
            raise BackendUnavailableError("API services are not configured")

        return unavailable


class CogniStoreGateway:
    """Application services composed from storage, catalog, search, and queue contracts."""

    def __init__(
        self,
        catalog: CatalogStore,
        drivers: Mapping[str, StorageDriver],
        *,
        ask_service: AskService | None = None,
        queue: JobQueue | None = None,
        manage_queue: bool = False,
    ) -> None:
        self.catalog = catalog
        self.drivers = dict(drivers)
        self.ask_service = ask_service or AskService(catalog)
        self.queue = queue
        self.manage_queue = manage_queue

    async def startup(self) -> None:
        if self.manage_queue and self.queue is not None:
            await self.queue.connect()

    async def shutdown(self) -> None:
        if self.manage_queue and self.queue is not None:
            await self.queue.close(graceful=True)

    def _driver(self, tier: str) -> StorageDriver:
        try:
            return self.drivers[tier]
        except KeyError as exc:
            raise ResourceNotFoundError("tier", tier) from exc

    def _catalog_record(self, bucket: str, key: str) -> ObjectRecord:
        record = self.catalog.get(bucket, key)
        if record is None:
            raise ResourceNotFoundError("catalog object", f"{bucket}/{key}")
        return record

    @staticmethod
    def _json_mapping(value: Mapping[str, object]) -> dict[str, JSONValue]:
        try:
            encoded = json.dumps(
                dict(value),
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            decoded = json.loads(encoded)
        except (TypeError, ValueError, RecursionError) as exc:
            raise RuntimeError("backend returned non-JSON metadata") from exc
        if not isinstance(decoded, dict):  # pragma: no cover - Mapping guarantees this
            raise RuntimeError("backend returned non-object metadata")
        return cast(dict[str, JSONValue], decoded)

    @staticmethod
    def _public_metadata(
        value: Mapping[str, object],
    ) -> tuple[dict[str, JSONValue], bool]:
        candidate = {
            name: item
            for name, item in value.items()
            if name not in _INTERNAL_OBJECT_METADATA_KEYS
        }
        try:
            encoded = json.dumps(
                candidate,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise RuntimeError("backend returned non-JSON metadata") from exc
        if len(encoded.encode("ascii")) > _MAX_PUBLIC_METADATA_BYTES:
            core = {
                name: candidate[name]
                for name in ("mime", "sha256")
                if name in candidate
            }
            try:
                core_encoded = json.dumps(
                    core,
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            except (TypeError, ValueError, RecursionError):
                return {}, True
            if len(core_encoded.encode("ascii")) > _MAX_PUBLIC_METADATA_BYTES:
                return {}, True
            core_decoded = json.loads(core_encoded)
            return cast(dict[str, JSONValue], core_decoded), True
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):  # pragma: no cover - comprehension guarantees it
            raise RuntimeError("backend returned non-object metadata")
        return cast(dict[str, JSONValue], decoded), False

    @staticmethod
    def _storage_metadata(
        stat: Mapping[str, object], *, content_type: str | None = None
    ) -> dict[str, object]:
        metadata: dict[str, object] = {}
        for name in ("mtime", "etag", "version_id"):
            value = stat.get(name)
            if isinstance(value, (str, int, float, bool)) or value is None:
                metadata[name] = value
        backend_metadata = stat.get("metadata")
        if isinstance(backend_metadata, Mapping):
            metadata["backend_metadata"] = {
                str(name): value
                for name, value in backend_metadata.items()
                if isinstance(value, (str, int, float, bool)) or value is None
            }
        media_type = content_type or stat.get("content_type")
        if isinstance(media_type, str) and media_type:
            metadata["mime"] = media_type.split(";", 1)[0].strip()
        return metadata

    @staticmethod
    def _generation(stat: Mapping[str, object], bucket: str, key: str) -> str:
        generation = stat.get("generation")
        if not isinstance(generation, str) or not generation:
            raise RuntimeError(f"storage returned no generation for {bucket}/{key}")
        return generation

    def _object_resource(
        self,
        tier: str,
        bucket: str,
        key: str,
        stat: Mapping[str, object],
        metadata: Mapping[str, object],
    ) -> ObjectResource:
        size = stat.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RuntimeError(f"storage returned an invalid size for {bucket}/{key}")
        public_metadata, metadata_truncated = self._public_metadata(metadata)
        return ObjectResource(
            tier=tier,
            bucket=bucket,
            key=key,
            size=size,
            generation=self._generation(stat, bucket, key),
            metadata=public_metadata,
            metadata_truncated=metadata_truncated,
        )

    def put_object(
        self,
        tier: str,
        bucket: str,
        key: str,
        data: bytes,
        *,
        overwrite: bool,
        content_type: str | None,
    ) -> ObjectResource:
        driver = self._driver(tier)
        driver.put_object(bucket, key, data, overwrite=overwrite)
        stat = driver.stat_object(bucket, key)
        metadata = self._storage_metadata(stat, content_type=content_type)
        self.catalog.upsert(bucket, key, len(data), tier, metadata=metadata)
        return self._object_resource(tier, bucket, key, stat, metadata)

    def stat_object(self, tier: str, bucket: str, key: str) -> ObjectResource:
        record = self._catalog_record(bucket, key)
        if record.tier != tier:
            raise ResourceNotFoundError("object", f"{tier}/{bucket}/{key}")
        stat = self._driver(tier).stat_object(bucket, key)
        return self._object_resource(tier, bucket, key, stat, record.metadata)

    def open_object(
        self,
        tier: str,
        bucket: str,
        key: str,
        *,
        byte_range: str | None,
    ) -> ObjectDownload:
        resource = self.stat_object(tier, bucket, key)
        driver = self._driver(tier)
        normalized_range: str | None = None
        content_range: str | None = None
        content_length = resource.size
        if byte_range is not None:
            if not driver.capabilities.range_reads:
                raise RangeNotSatisfiableError(resource.size)
            start, end = self._parse_byte_range(byte_range, resource.size)
            normalized_range = f"bytes={start}-{end}"
            content_range = f"bytes {start}-{end}/{resource.size}"
            content_length = end - start + 1

        def chunks() -> Generator[bytes, None, None]:
            with driver.open_object_reader_if_generation(
                bucket,
                key,
                resource.generation,
                range=normalized_range,
            ) as reader:
                # Open the generation-bound backend request and expose one
                # byte before HTTP headers are committed. Connection and
                # precondition failures can then use the normal JSON envelope.
                first = reader.read(1)
                if first:
                    yield first
                while chunk := reader.read(DEFAULT_STREAM_CHUNK_SIZE):
                    yield chunk

        content_type = resource.metadata.get("mime", "application/octet-stream")
        if not isinstance(content_type, str):
            content_type = "application/octet-stream"
        source = chunks()
        try:
            first_chunk = next(source)
        except StopIteration:
            first_chunk = None

        def prefetched_chunks() -> Iterator[bytes]:
            try:
                if first_chunk is not None:
                    yield first_chunk
                yield from source
            finally:
                source.close()

        return ObjectDownload(
            resource,
            prefetched_chunks(),
            content_type,
            content_length,
            content_range,
            source.close,
        )

    @staticmethod
    def _parse_byte_range(value: str, size: int) -> tuple[int, int]:
        match = _BYTE_RANGE.fullmatch(value.strip())
        if match is None or size == 0:
            raise RangeNotSatisfiableError(size)
        start_text, end_text = match.groups()
        if not start_text:
            if not end_text:
                raise RangeNotSatisfiableError(size)
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise RangeNotSatisfiableError(size)
            return max(0, size - suffix_length), size - 1

        start = int(start_text)
        if start >= size:
            raise RangeNotSatisfiableError(size)
        if not end_text:
            return start, size - 1
        end = int(end_text)
        if end < start:
            raise RangeNotSatisfiableError(size)
        return start, min(end, size - 1)

    def delete_object(self, tier: str, bucket: str, key: str) -> None:
        resource = self.stat_object(tier, bucket, key)
        deleted = self._driver(tier).delete_object_if_generation(
            bucket,
            key,
            resource.generation,
        )
        if not deleted:
            raise ResourceNotFoundError("object", f"{tier}/{bucket}/{key}")
        self.catalog.delete(bucket, key)

    def get_catalog_object(self, bucket: str, key: str) -> CatalogObject:
        return self._catalog_response(self._catalog_record(bucket, key))

    def _catalog_response(self, record: ObjectRecord) -> CatalogObject:
        public_metadata, metadata_truncated = self._public_metadata(record.metadata)
        return CatalogObject(
            bucket=record.bucket,
            key=record.key,
            size=record.size,
            tier=record.tier,
            metadata=public_metadata,
            metadata_truncated=metadata_truncated,
        )

    def list_catalog_objects(
        self,
        *,
        bucket: str,
        prefix: str,
        tier: str | None,
        limit: int,
        cursor: str | None,
    ) -> CatalogObjectPage:
        filters = {"bucket": bucket, "prefix": prefix, "tier": tier}
        try:
            after_key = decode_cursor(
                cursor,
                resource=_CATALOG_CURSOR_RESOURCE,
                filters=filters,
            )
        except CursorError as exc:
            raise RequestContractError(str(exc), code="invalid_cursor") from exc
        records = self.catalog.list_page(
            bucket,
            prefix,
            after_key=after_key,
            limit=limit + 1,
            tier=tier,
        )
        has_more = len(records) > limit
        selected = records[:limit]
        next_cursor = (
            encode_cursor(
                resource=_CATALOG_CURSOR_RESOURCE,
                filters=filters,
                last_key=selected[-1].key,
            )
            if has_more and selected
            else None
        )
        return CatalogObjectPage(
            items=[self._catalog_response(record) for record in selected],
            page=PageMetadata(limit=limit, next_cursor=next_cursor),
        )

    def ask(self, request: AskRequest) -> AskResponse:
        filters = request.filters
        try:
            query = AskQuery(
                text=request.text,
                filters=AskFilters(
                    bucket=filters.bucket,
                    key_prefix=filters.key_prefix,
                    tier=filters.tier,
                    mime=filters.mime,
                    size=filters.size,
                    content_sha256=filters.content_sha256,
                    object_metadata=filters.object_metadata,
                    document_metadata=filters.document_metadata,
                ),
                limit=request.limit,
                candidate_limit=request.candidate_limit,
                passages_per_result=request.passages_per_result,
                synthesize=request.synthesize,
                exact_vector=request.exact_vector,
            )
        except ValueError as exc:
            # Only construction is request-derived. Keep the service call out
            # of this block so provider and backend ValueErrors remain private
            # 500 responses instead of being misclassified as client input.
            raise RequestContractError(str(exc)) from exc
        domain_response = self.ask_service.ask(query)
        return AskResponse(
            schema_version=1,
            mode=cast(RetrievalModeValue, domain_response.mode.value),
            active_signals=cast(
                list[RetrievalSignalValue],
                [signal.value for signal in domain_response.active_signals],
            ),
            results=[
                RetrievalResultResponse(
                    citation=ObjectCitationResponse(
                        citation_id=result.citation.citation_id,
                        object_id=result.citation.object_id,
                        bucket=result.citation.bucket,
                        key=result.citation.key,
                        tier=result.citation.tier,
                        size=result.citation.size,
                        mime=result.citation.mime,
                        content_sha256=result.citation.content_sha256,
                        object_metadata=self._json_mapping(
                            result.citation.object_metadata
                        ),
                        document_metadata=self._json_mapping(
                            result.citation.document_metadata
                        ),
                        object_metadata_truncated=(
                            result.citation.object_metadata_truncated
                        ),
                        document_metadata_truncated=(
                            result.citation.document_metadata_truncated
                        ),
                    ),
                    score=result.score,
                    score_components=[
                        ScoreComponentResponse(
                            signal=cast(RetrievalSignalValue, component.signal.value),
                            rank=component.rank,
                            raw_score=component.raw_score,
                            weight=component.weight,
                            contribution=component.contribution,
                        )
                        for component in result.score_components
                    ],
                    passages=[
                        RetrievedPassageResponse(
                            citation=PassageCitationResponse(
                                citation_id=passage.citation.citation_id,
                                object_id=passage.citation.object_id,
                                bucket=passage.citation.bucket,
                                key=passage.citation.key,
                                source=cast(
                                    PassageSignalValue, passage.citation.source.value
                                ),
                                passage_id=passage.citation.passage_id,
                                passage_index=passage.citation.passage_index,
                                start_codepoint=passage.citation.start_codepoint,
                                end_codepoint=passage.citation.end_codepoint,
                                text_sha256=passage.citation.text_sha256,
                                source_sha256=passage.citation.source_sha256,
                                document_text_sha256=(
                                    passage.citation.document_text_sha256
                                ),
                                document_id=passage.citation.document_id,
                                space_id=passage.citation.space_id,
                            ),
                            text=passage.text,
                            match=PassageMatchResponse(
                                signal=cast(
                                    PassageSignalValue, passage.match.signal.value
                                ),
                                rank=passage.match.rank,
                                raw_score=passage.match.raw_score,
                            ),
                        )
                        for passage in result.passages
                    ],
                )
                for result in domain_response.results
            ],
            providers=[
                ProviderDiagnosticResponse(
                    component=cast(
                        Literal["metadata", "keyword", "vector", "generation"],
                        provider.component,
                    ),
                    state=cast(
                        Literal[
                            "succeeded",
                            "missing",
                            "unavailable",
                            "not_requested",
                            "no_context",
                        ],
                        provider.state.value,
                    ),
                    error_type=provider.error_type,
                )
                for provider in domain_response.providers
            ],
            generation_status=cast(
                Literal[
                    "not_requested",
                    "provider_missing",
                    "no_context",
                    "succeeded",
                    "provider_unavailable",
                ],
                domain_response.generation_status.value,
            ),
            answer=(
                None
                if domain_response.answer is None
                else GeneratedAnswerResponse(
                    text=domain_response.answer.text,
                    citations=list(domain_response.answer.citations),
                )
            ),
        )

    def _validate_policy_tiers(self, config: PolicyConfig) -> None:
        unknown = sorted(set(config.allowed_tiers).difference(self.drivers))
        if unknown:
            raise RequestContractError(
                f"unknown allowed tier(s): {', '.join(unknown)}"
            )

    @staticmethod
    def _policy(config: PolicyConfig):
        return build_policy(
            config.policy,
            threshold=config.threshold,
            llm_threshold=config.llm_threshold,
            allowed_tiers=config.allowed_tiers,
            hot_name_patterns=config.hot_name_patterns,
            warm_name_patterns=config.warm_name_patterns,
            cold_name_patterns=config.cold_name_patterns,
            hot_mime_prefixes=config.hot_mime_prefixes,
            warm_mime_prefixes=config.warm_mime_prefixes,
            cold_mime_prefixes=config.cold_mime_prefixes,
        )

    def evaluate_policy(
        self, request: PolicyEvaluationRequest
    ) -> PolicyEvaluationResponse:
        self._validate_policy_tiers(request.config)
        record = self._catalog_record(request.bucket, request.key)
        policy = self._policy(request.config)
        evaluate_record = getattr(policy, "evaluate_record", None)
        decision = (
            evaluate_record(record)
            if callable(evaluate_record)
            else policy.evaluate(record.tier, record.size)
        )
        return PolicyEvaluationResponse(
            bucket=record.bucket,
            key=record.key,
            current_tier=record.tier,
            size=record.size,
            action=decision.action,
            destination_tier=decision.dst_tier,
            reason=decision.reason,
        )

    def _queue(self) -> JobQueue:
        if self.queue is None:
            raise BackendUnavailableError("The asynchronous job queue is not configured")
        return self.queue

    async def _append_status_event(
        self,
        job: JobEnvelope,
        event_type: AuditEventType,
        outcome: AuditOutcome,
        *,
        details: Mapping[str, object],
    ) -> AuditEvent:
        event = AuditEvent.create(
            event_type,
            outcome,
            AuditContext(
                correlation_id=job.correlation_id,
                actor_type="api",
                actor_id="cognistore-rest-api",
                job_id=job.job_id,
            ),
            event_id=stable_audit_event_id(
                "api-job-status", event_type.value, job.job_id
            ),
            occurred_at=(
                job.created_at
                if event_type is AuditEventType.JOB_QUEUED
                else None
            ),
            details={"job_type": job.job_type, **details},
        )
        return await asyncio.to_thread(self.catalog.append_audit_event, event)

    async def _submit(self, job: JobEnvelope) -> JobStatusResponse:
        await self._append_status_event(
            job,
            AuditEventType.JOB_QUEUED,
            AuditOutcome.REQUESTED,
            details={},
        )
        try:
            await self._queue().enqueue(job, message_id=job.job_id)
        except Exception as exc:
            try:
                await self._append_status_event(
                    job,
                    AuditEventType.JOB_SUBMISSION_FAILED,
                    AuditOutcome.FAILED,
                    details={
                        "error_type": type(exc).__name__,
                        "retryable": bool(getattr(exc, "retryable", False)),
                    },
                )
            except Exception:
                pass
            raise
        return await asyncio.to_thread(self.get_job, job.job_id)

    async def submit_catalog_scan(
        self, request: CatalogScanRequest
    ) -> JobStatusResponse:
        self._driver(request.tier)
        job = JobEnvelope.create(
            CATALOG_SCAN_JOB,
            {
                "tier": request.tier,
                "bucket": request.bucket,
                "prefix": request.prefix,
            },
            metadata={STATUS_TRACKING_METADATA: "1"},
        )
        return await self._submit(job)

    async def submit_policy_run(
        self, request: PolicyRunRequest
    ) -> JobStatusResponse:
        self._validate_policy_tiers(request.config)
        config = request.config
        job = JobEnvelope.create(
            POLICY_RUN_JOB,
            policy_job_payload(
                bucket=request.bucket,
                prefix=request.prefix,
                policy=config.policy,
                threshold=config.threshold,
                llm_threshold=config.llm_threshold,
                allowed_tiers=config.allowed_tiers,
                hot_name_patterns=config.hot_name_patterns,
                warm_name_patterns=config.warm_name_patterns,
                cold_name_patterns=config.cold_name_patterns,
                hot_mime_prefixes=config.hot_mime_prefixes,
                warm_mime_prefixes=config.warm_mime_prefixes,
                cold_mime_prefixes=config.cold_mime_prefixes,
            ),
            metadata={STATUS_TRACKING_METADATA: "1"},
        )
        return await self._submit(job)

    @staticmethod
    def _detail_integer(event: AuditEvent, name: str) -> int:
        value = event.details.get(name, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    @classmethod
    def _status_event_key(cls, event: AuditEvent) -> tuple[int, str, int]:
        """Order lifecycle evidence by attempt, time, then transition phase."""

        attempt = max(
            cls._detail_integer(event, "attempt"),
            cls._detail_integer(event, "cumulative_attempt"),
        )
        phase = {
            AuditEventType.JOB_QUEUED.value: 0,
            AuditEventType.JOB_STARTED.value: 1,
            AuditEventType.JOB_FAILURE.value: 2,
            AuditEventType.JOB_RETRY.value: 3,
            AuditEventType.JOB_SUBMISSION_FAILED.value: 4,
            AuditEventType.JOB_DEAD_LETTERED.value: 4,
            AuditEventType.JOB_SUCCEEDED.value: 5,
        }.get(event.event_type, 0)
        return attempt, event.occurred_at, phase

    def get_job(self, job_id: str) -> JobStatusResponse:
        # Read the newest bounded window so a long retry/redrive history cannot
        # hide a terminal transition beyond an ascending query's limit. Fetch
        # the creation event separately because it owns immutable job metadata.
        events = self.catalog.list_audit_events(
            AuditQuery(
                job_id=job_id,
                event_types=_STATUS_EVENT_TYPES,
                limit=1000,
                ascending=False,
            )
        )
        queued_events = self.catalog.list_audit_events(
            AuditQuery(
                job_id=job_id,
                event_types=frozenset({AuditEventType.JOB_QUEUED.value}),
                limit=1,
            )
        )
        queued = queued_events[0] if queued_events else None
        if queued is None:
            raise ResourceNotFoundError("job", job_id)

        lifecycle_events = {event.event_id: event for event in events}
        lifecycle_events[queued.event_id] = queued
        ordered_events = sorted(
            lifecycle_events.values(),
            key=self._status_event_key,
            reverse=True,
        )
        latest = ordered_events[0]
        attempt = max(
            (
                max(
                    self._detail_integer(event, "attempt"),
                    self._detail_integer(event, "cumulative_attempt"),
                )
                for event in ordered_events
            ),
            default=0,
        )
        if latest.event_type == AuditEventType.JOB_SUCCEEDED.value:
            status: JobState = "succeeded"
        elif latest.event_type in {
            AuditEventType.JOB_SUBMISSION_FAILED.value,
            AuditEventType.JOB_DEAD_LETTERED.value,
        }:
            status = "failed"
        elif latest.event_type == AuditEventType.JOB_RETRY.value:
            status = "retrying"
        elif latest.event_type in {
            AuditEventType.JOB_STARTED.value,
            AuditEventType.JOB_FAILURE.value,
        }:
            status = "running"
        else:
            status = "queued"

        retryable = None
        error_type = None
        if status in {"retrying", "failed"}:
            diagnostic_events = [latest] + [
                event
                for event in ordered_events
                if event.event_type
                in {
                    AuditEventType.JOB_SUBMISSION_FAILED.value,
                    AuditEventType.JOB_FAILURE.value,
                    AuditEventType.JOB_DEAD_LETTERED.value,
                }
            ]
            for event in diagnostic_events:
                if retryable is None:
                    retryable_value = event.details.get("retryable")
                    if isinstance(retryable_value, bool):
                        retryable = retryable_value
                if error_type is None:
                    raw_error_type = event.details.get(
                        "error_type"
                    ) or event.details.get("exception_type")
                    if isinstance(raw_error_type, str):
                        error_type = raw_error_type.rsplit(".", 1)[-1]
                if retryable is not None and error_type is not None:
                    break

        job_type = queued.details.get("job_type")
        if job_type not in {CATALOG_SCAN_JOB, POLICY_RUN_JOB}:
            raise RuntimeError("queued job status has an invalid job type")
        try:
            parsed_job_id = UUID(job_id)
            parsed_correlation_id = UUID(queued.correlation_id)
            created_at = datetime.fromisoformat(
                queued.occurred_at.replace("Z", "+00:00")
            )
            updated_at = datetime.fromisoformat(
                latest.occurred_at.replace("Z", "+00:00")
            )
        except ValueError as exc:  # persisted audit data violated its contract
            raise RuntimeError("job status contains invalid persisted data") from exc
        return JobStatusResponse(
            job_id=parsed_job_id,
            correlation_id=parsed_correlation_id,
            job_type=cast(Literal["catalog.scan", "policy.run"], job_type),
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            status_url=f"/v1/jobs/{job_id}",
            attempt=attempt,
            retryable=retryable,
            error_type=error_type,
        )
