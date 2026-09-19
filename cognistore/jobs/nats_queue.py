from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import nats
from nats.errors import NoRespondersError
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import (
    DEFAULT_PREFIX,
    AckPolicy,
    ConsumerConfig,
    DeliverPolicy,
    DiscardPolicy,
    Header,
    ReplayPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.errors import APIError, NotFoundError, ServiceUnavailableError

from cognistore.encryption import production_mode, require_at_rest, require_tls_url, tls_context
from cognistore.observability import (
    inject_trace_context,
    instrument,
    observe,
    record_job_event,
    request_context,
)

from .models import (
    ATTEMPT_OFFSET_METADATA,
    DEAD_LETTER_CHAIN_METADATA,
    REDRIVE_COUNT_METADATA,
    REDRIVE_SCHEMA_VERSION,
    REDRIVEN_FROM_METADATA,
    BusState,
    DeadLetterDisposition,
    DeadLetterNotFoundError,
    DeadLetterReceipt,
    DeadLetterRecord,
    DeadLetterRecordError,
    DeadLetterRedriveError,
    EnqueueReceipt,
    JobEnvelope,
    JobEnvelopeError,
    QueueHealth,
    QueueSaturatedError,
    RedriveAuditRecord,
    RedriveReceipt,
)
from .telemetry import job_operation as _job_operation

LOGGER = logging.getLogger(__name__)
CORRELATION_HEADER = "CogniStore-Correlation-Id"
JOB_TYPE_HEADER = "CogniStore-Job-Type"
DEAD_LETTER_ID_HEADER = "CogniStore-Dead-Letter-Id"
DEAD_LETTER_DISPOSITION_HEADER = "CogniStore-Dead-Letter-Disposition"
DEAD_LETTER_CATEGORY_HEADER = "CogniStore-Dead-Letter-Category"
REDRIVE_PARENT_HEADER = "CogniStore-Redrive-Parent"
REDRIVE_COUNT_HEADER = "CogniStore-Redrive-Count"
CONSUMER_ALREADY_EXISTS = 10148
MINIMUM_NATS_SERVER = (2, 10)
DEFAULT_DEAD_LETTER_MAX_AGE = 30 * 24 * 60 * 60
DEFAULT_STREAM_MAX_MESSAGES = 10_000
DEFAULT_STREAM_MAX_BYTES = 1024 * 1024 * 1024
DEAD_LETTER_STORAGE_SCHEMA_VERSION = 1
DEAD_LETTER_CHUNK_TARGET = 256 * 1024
MAX_PROJECTED_SEQUENCE = 2**64 - 1
MAX_PROJECTED_TIMESTAMP = "9999-12-31T23:59:59.999999Z"
STREAM_STORE_FAILED = 10077
QUEUE_SATURATION_REASONS = frozenset(
    {"maximum messages exceeded", "maximum bytes exceeded"}
)


def _is_utf8_encodable(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


async def _ignore_connection_error(_error: Exception) -> None:
    """Suppress nats-py's traceback callback for bounded one-shot commands."""


async def _report_connection_error(_error: Exception) -> None:
    """Keep server addresses, credentials, and broker exceptions out of logs."""
    LOGGER.warning("NATS connection failed; reconnecting when configured")


@dataclass(frozen=True)
class NatsJetStreamConfig:
    servers: tuple[str, ...] = ("nats://127.0.0.1:4222",)
    stream: str = "COGNISTORE_JOBS"
    subject: str = "cognistore.jobs"
    consumer: str = "cognistore-workers"
    ack_wait: float = 30.0
    max_ack_pending: int = 1
    duplicate_window: float = 120.0
    connect_timeout: float = 2.0
    reconnect_time_wait: float = 2.0
    max_reconnect_attempts: int = 60
    allow_reconnect: bool = True
    report_connection_errors: bool = True
    request_timeout: float = 5.0
    drain_timeout: float = 30.0
    client_name: str = "cognistore"
    dead_letter_stream: str | None = None
    dead_letter_subject: str | None = None
    dead_letter_max_age: float = DEFAULT_DEAD_LETTER_MAX_AGE
    # Appended to preserve the positional constructor used by older callers.
    stream_max_messages: int = DEFAULT_STREAM_MAX_MESSAGES
    stream_max_bytes: int = DEFAULT_STREAM_MAX_BYTES

    def __post_init__(self) -> None:
        if not self.servers or any(not value.strip() for value in self.servers):
            raise ValueError("at least one non-empty NATS server URL is required")
        for server in self.servers:
            require_tls_url(server, "NATS", schemes=("tls",))
        for field_name in ("stream", "subject", "consumer", "client_name"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must be non-empty")
        for field_name in ("dead_letter_stream", "dead_letter_subject"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field_name} must be non-empty when provided")
        for field_name in (
            "ack_wait",
            "duplicate_window",
            "connect_timeout",
            "reconnect_time_wait",
            "request_timeout",
            "drain_timeout",
            "dead_letter_max_age",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be greater than zero")
        if self.max_ack_pending < 1:
            raise ValueError("max_ack_pending must be at least one")
        for field_name in ("stream_max_messages", "stream_max_bytes"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        if (
            not isinstance(self.max_reconnect_attempts, int)
            or isinstance(self.max_reconnect_attempts, bool)
            or self.max_reconnect_attempts < -1
        ):
            raise ValueError("max_reconnect_attempts must be -1 or a non-negative integer")
        if not isinstance(self.allow_reconnect, bool):
            raise ValueError("allow_reconnect must be a boolean")
        if not isinstance(self.report_connection_errors, bool):
            raise ValueError("report_connection_errors must be a boolean")
        if self.dead_letter_max_age < self.duplicate_window:
            raise ValueError(
                "dead_letter_max_age must be at least duplicate_window"
            )

    @property
    def resolved_dead_letter_stream(self) -> str:
        return self.dead_letter_stream or f"{self.stream}_DLQ"

    @property
    def resolved_dead_letter_subject(self) -> str:
        return self.dead_letter_subject or f"{self.subject}.dead"


class NatsJobDelivery:
    """A single JetStream delivery with serialized terminal settlement."""

    def __init__(
        self,
        message: Any,
        ack_timeout: float,
        *,
        source_stream: str,
        source_consumer: str,
    ) -> None:
        metadata = message.metadata
        self._message = message
        self._ack_timeout = ack_timeout
        self._lock = asyncio.Lock()
        self._settled = False
        self._job: JobEnvelope | None = None
        self._job_error: JobEnvelopeError | None = None
        self._parsed = False
        self.raw_data = bytes(message.data)
        raw_headers = message.headers or {}
        self.headers = {str(key): str(value) for key, value in raw_headers.items()}
        self.attempt = metadata.num_delivered
        self.source_stream = getattr(metadata, "stream", None) or source_stream
        self.source_consumer = getattr(metadata, "consumer", None) or source_consumer
        self.source_published_at = metadata.timestamp
        self.stream_sequence = metadata.sequence.stream
        self.consumer_sequence = metadata.sequence.consumer

    @property
    def job(self) -> JobEnvelope:
        if not self._parsed:
            self._parsed = True
            try:
                self._job = JobEnvelope.from_bytes(self.raw_data)
            except JobEnvelopeError as exc:
                self._job_error = exc
        if self._job_error is not None:
            raise self._job_error
        assert self._job is not None
        return self._job

    @property
    def envelope_error(self) -> JobEnvelopeError | None:
        try:
            self.job
        except JobEnvelopeError:
            pass
        return self._job_error

    @property
    def settled(self) -> bool:
        return self._settled

    async def ack(self) -> None:
        async with self._lock:
            if self._settled:
                return
            # Double acknowledgement means success is reported only after the
            # server confirms it recorded the ACK.
            await self._message.ack_sync(timeout=self._ack_timeout)
            self._settled = True

    async def nack(self, delay: float | None = None) -> None:
        async with self._lock:
            if self._settled:
                return
            if delay is None:
                await self._message.nak()
            else:
                await self._message.nak(delay=delay)
            self._settled = True

    async def in_progress(self) -> None:
        async with self._lock:
            if not self._settled:
                await self._message.in_progress()


class NatsJetStreamQueue:
    """Durable, at-least-once job queue backed by NATS JetStream."""

    def __init__(self, config: NatsJetStreamConfig, *, consume: bool = True) -> None:
        require_at_rest("queue")
        self.config = config
        self._consume = consume
        self._connection: Any | None = None
        self._jetstream: Any | None = None
        self._subscription: Any | None = None
        self._dead_letter_ready = False
        self._stream_max_msg_sizes: dict[str, int] = {}

    async def connect(self) -> None:
        if self._connection is not None and not self._connection.is_closed:
            return
        require_at_rest("queue")
        for server in self.config.servers:
            require_tls_url(server, "NATS", schemes=("tls",))
        connection_options: dict[str, Any] = {
            "error_cb": (
                _report_connection_error
                if self.config.report_connection_errors
                else _ignore_connection_error
            )
        }
        if production_mode():
            # nats-py otherwise trusts a plaintext INFO's tls_required flag,
            # even for tls:// URLs. A TLS-first handshake also protects every
            # reconnect, including the broker's discovered cluster members.
            connection_options.update(tls=tls_context(), tls_handshake_first=True)
        connection = await nats.connect(
            servers=list(self.config.servers),
            name=self.config.client_name,
            connect_timeout=self.config.connect_timeout,
            reconnect_time_wait=self.config.reconnect_time_wait,
            max_reconnect_attempts=self.config.max_reconnect_attempts,
            allow_reconnect=self.config.allow_reconnect,
            drain_timeout=self.config.drain_timeout,
            **connection_options,
        )
        self._connection = connection
        self._jetstream = connection.jetstream(timeout=self.config.request_timeout)
        try:
            await self._ensure_stream()
            if self._consume:
                await self._ensure_dead_letter_stream()
                await self._ensure_consumer()
                self._subscription = await self._jetstream.pull_subscribe_bind(
                    consumer=self.config.consumer,
                    stream=self.config.stream,
                )
        except BaseException:
            await connection.close()
            self._connection = None
            self._jetstream = None
            self._subscription = None
            self._dead_letter_ready = False
            self._stream_max_msg_sizes.clear()
            raise

    async def _ensure_stream(self) -> None:
        assert self._jetstream is not None
        try:
            stream_info = await self._jetstream.stream_info(self.config.stream)
        except NotFoundError:
            try:
                stream_info = await self._jetstream.add_stream(
                    config=StreamConfig(
                        name=self.config.stream,
                        description="Durable CogniStore background jobs",
                        subjects=[self.config.subject],
                        retention=RetentionPolicy.WORK_QUEUE,
                        storage=StorageType.FILE,
                        max_msgs=self.config.stream_max_messages,
                        max_bytes=self.config.stream_max_bytes,
                        max_age=0,
                        max_msgs_per_subject=-1,
                        discard=DiscardPolicy.NEW,
                        duplicate_window=self.config.duplicate_window,
                    )
                )
            except APIError as create_error:
                # Multiple first workers may observe the missing stream at the
                # same time. Validate the winner's configuration below.
                try:
                    stream_info = await self._jetstream.stream_info(self.config.stream)
                except NotFoundError:
                    raise create_error
        stream_config = stream_info.config
        if not self._main_stream_config_compatible(stream_config):
            raise RuntimeError(
                f"NATS stream {self.config.stream!r} has incompatible configuration"
            )
        self._stream_max_msg_sizes[self.config.stream] = int(
            stream_config.max_msg_size or -1
        )

    def _main_stream_config_compatible(self, stream_config: StreamConfig) -> bool:
        persist_mode = getattr(stream_config, "persist_mode", None)
        persist_mode_value = getattr(persist_mode, "value", persist_mode)
        return not (
            stream_config.name != self.config.stream
            or stream_config.retention != RetentionPolicy.WORK_QUEUE
            or stream_config.storage != StorageType.FILE
            or set(stream_config.subjects or ()) != {self.config.subject}
            or stream_config.max_msgs != self.config.stream_max_messages
            or stream_config.max_bytes != self.config.stream_max_bytes
            or stream_config.max_age not in (None, 0)
            or stream_config.max_msgs_per_subject != -1
            or stream_config.discard != DiscardPolicy.NEW
            or bool(getattr(stream_config, "discard_new_per_subject", False))
            or bool(getattr(stream_config, "no_ack", False))
            or bool(getattr(stream_config, "sealed", False))
            or bool(getattr(stream_config, "allow_rollup_hdrs", False))
            or bool(getattr(stream_config, "allow_msg_ttl", False))
            or getattr(stream_config, "subject_transform", None) is not None
            or getattr(stream_config, "mirror", None) is not None
            or bool(getattr(stream_config, "sources", None))
            or persist_mode_value not in (None, "default")
            or stream_config.duplicate_window is None
            or abs(
                stream_config.duplicate_window - self.config.duplicate_window
            )
            > 0.001
        )

    async def _ensure_dead_letter_stream(self) -> None:
        if self._dead_letter_ready:
            return
        assert self._jetstream is not None
        stream = self.config.resolved_dead_letter_stream
        subject = f"{self.config.resolved_dead_letter_subject}.>"
        try:
            stream_info = await self._jetstream.stream_info(stream)
        except NotFoundError:
            try:
                stream_info = await self._jetstream.add_stream(
                    config=StreamConfig(
                        name=stream,
                        description=(
                            "Immutable CogniStore failed-job diagnostics and redrive audit"
                        ),
                        subjects=[subject],
                        retention=RetentionPolicy.LIMITS,
                        storage=StorageType.FILE,
                        duplicate_window=self.config.duplicate_window,
                        max_age=self.config.dead_letter_max_age,
                        max_msgs=-1,
                        max_bytes=-1,
                        max_msgs_per_subject=-1,
                        max_msg_size=-1,
                        discard=DiscardPolicy.OLD,
                    )
                )
            except APIError as create_error:
                try:
                    stream_info = await self._jetstream.stream_info(stream)
                except NotFoundError:
                    raise create_error
        stream_config = stream_info.config
        if (
            stream_config.name != stream
            or stream_config.retention != RetentionPolicy.LIMITS
            or stream_config.storage != StorageType.FILE
            or set(stream_config.subjects or ()) != {subject}
            or stream_config.max_msgs != -1
            or stream_config.max_bytes != -1
            or stream_config.max_msgs_per_subject != -1
            or stream_config.max_msg_size != -1
            or stream_config.discard != DiscardPolicy.OLD
            or stream_config.duplicate_window is None
            or abs(stream_config.duplicate_window - self.config.duplicate_window) > 0.001
            or stream_config.max_age is None
            or abs(stream_config.max_age - self.config.dead_letter_max_age) > 0.001
        ):
            raise RuntimeError(f"NATS stream {stream!r} has incompatible configuration")
        self._stream_max_msg_sizes[stream] = int(stream_config.max_msg_size or -1)
        self._dead_letter_ready = True

    async def _ensure_consumer(self) -> None:
        assert self._jetstream is not None
        assert self._connection is not None
        version = self._connection.connected_server_version
        if (version.major, version.minor) < MINIMUM_NATS_SERVER:
            raise RuntimeError(
                "CogniStore workers require NATS Server 2.10 or newer for "
                "atomic durable-consumer creation"
            )

        desired = ConsumerConfig(
            name=self.config.consumer,
            durable_name=self.config.consumer,
            description="Shared CogniStore worker consumer",
            filter_subject=self.config.subject,
            deliver_policy=DeliverPolicy.ALL,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=self.config.ack_wait,
            max_deliver=-1,
            max_ack_pending=self.config.max_ack_pending,
            replay_policy=ReplayPolicy.INSTANT,
        )
        # nats-py currently exposes only create-or-update. Use JetStream's
        # server-side create action so two first-starting workers cannot race
        # and rewrite each other's AckWait while work is in flight.
        request = {
            "stream_name": self.config.stream,
            "config": desired.as_dict(),
            "action": "create",
        }
        subject = (
            f"{DEFAULT_PREFIX}.CONSUMER.CREATE."
            f"{self.config.stream}.{self.config.consumer}.{self.config.subject}"
        )
        try:
            try:
                response_message = await self._connection.request(
                    subject,
                    json.dumps(request).encode("utf-8"),
                    timeout=self.config.request_timeout,
                )
            except NoRespondersError as exc:
                raise ServiceUnavailableError() from exc
            response = json.loads(response_message.data)
            if not isinstance(response, dict):
                raise RuntimeError("NATS consumer-create response must be an object")
            error = response.get("error")
            if error is not None:
                if not isinstance(error, dict):
                    raise RuntimeError("NATS consumer-create error must be an object")
                APIError.from_error(error)
        except APIError as exc:
            if exc.err_code != CONSUMER_ALREADY_EXISTS:
                raise

        consumer_info = await self._jetstream.consumer_info(
            self.config.stream, self.config.consumer
        )
        consumer_config = consumer_info.config
        if not self._consumer_config_compatible(consumer_config):
            raise RuntimeError(
                f"NATS consumer {self.config.consumer!r} has incompatible configuration"
            )

    def _consumer_config_compatible(self, consumer_config: ConsumerConfig) -> bool:
        return not (
            consumer_config.deliver_policy != DeliverPolicy.ALL
            or consumer_config.ack_policy != AckPolicy.EXPLICIT
            or consumer_config.filter_subject != self.config.subject
            or consumer_config.deliver_subject is not None
            or bool(getattr(consumer_config, "headers_only", False))
            or consumer_config.max_ack_pending != self.config.max_ack_pending
            or consumer_config.max_deliver != -1
            or bool(consumer_config.backoff)
            or consumer_config.replay_policy != ReplayPolicy.INSTANT
            or consumer_config.durable_name != self.config.consumer
            or consumer_config.name != self.config.consumer
            or consumer_config.ack_wait is None
            or abs(consumer_config.ack_wait - self.config.ack_wait) > 0.001
        )

    def _require_connected(self) -> tuple[Any, Any]:
        if self._connection is None or self._jetstream is None:
            raise RuntimeError("NATS queue is not connected")
        return self._connection, self._jetstream

    def _require_consumer(self) -> tuple[Any, Any, Any]:
        connection, jetstream = self._require_connected()
        if self._subscription is None:
            raise RuntimeError("NATS queue is not configured for claims")
        return connection, jetstream, self._subscription

    @staticmethod
    def _job_headers(job: JobEnvelope, transport_id: str) -> dict[Any, str]:
        headers = {
            Header.MSG_ID: transport_id,
            CORRELATION_HEADER: job.correlation_id,
            JOB_TYPE_HEADER: job.job_type,
        }
        # Only the W3C trace identifier is exported to transport headers.
        # Arbitrary metadata, baggage, and tracestate stay out of telemetry.
        parent = job.metadata.get("traceparent", "")
        if re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}", parent):
            if parent[3:35] != "0" * 32 and parent[36:52] != "0" * 16:
                headers["traceparent"] = parent
        return headers

    @staticmethod
    def _redrive_intent_headers(
        dead_letter_id: str, redrive_count: int, job: JobEnvelope
    ) -> dict[Any, str]:
        return {
            Header.MSG_ID: f"redrive-intent:{dead_letter_id}",
            Header.EXPECTED_LAST_SUBJECT_SEQUENCE: "0",
            DEAD_LETTER_ID_HEADER: dead_letter_id,
            REDRIVE_PARENT_HEADER: dead_letter_id,
            REDRIVE_COUNT_HEADER: str(redrive_count),
            CORRELATION_HEADER: job.correlation_id,
            JOB_TYPE_HEADER: job.job_type,
        }

    @staticmethod
    def _dead_letter_chunk_headers(
        dead_letter_id: str, record_digest: str, index: int
    ) -> dict[Any, str]:
        return {
            Header.MSG_ID: f"dead-letter-chunk:{dead_letter_id}:{record_digest}:{index}",
            Header.EXPECTED_LAST_SUBJECT_SEQUENCE: "0",
            DEAD_LETTER_ID_HEADER: dead_letter_id,
        }

    @staticmethod
    def _redrive_audit_headers(
        dead_letter_id: str,
        redrive_count: int,
        correlation_id: str,
        job_type: str,
    ) -> dict[Any, str]:
        return {
            Header.MSG_ID: f"redrive-audit:{dead_letter_id}",
            Header.EXPECTED_LAST_SUBJECT_SEQUENCE: "0",
            DEAD_LETTER_ID_HEADER: dead_letter_id,
            REDRIVE_PARENT_HEADER: dead_letter_id,
            REDRIVE_COUNT_HEADER: str(redrive_count),
            CORRELATION_HEADER: correlation_id,
            JOB_TYPE_HEADER: job_type,
        }

    @staticmethod
    def _nats_wire_size(
        payload: bytes, headers: dict[Any, str], *, stream: str
    ) -> int:
        # nats-py checks only len(payload), while the server applies max_payload
        # to HPUB's header block plus body. Mirror nats-py's exact encoding and
        # the Nats-Expected-Stream header added by JetStreamContext.publish.
        encoded_headers = dict(headers)
        encoded_headers[Header.EXPECTED_STREAM] = stream
        header_size = len(b"NATS/1.0\r\n\r\n")
        for key, value in encoded_headers.items():
            normalized_key = key.strip()
            normalized_value = value.strip()
            if any(character in normalized_key for character in "\r\n") or any(
                character in normalized_value for character in "\r\n"
            ):
                raise ValueError("NATS header keys and values must not contain CR or LF")
            try:
                encoded_key = normalized_key.encode("utf-8")
                encoded_value = normalized_value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("NATS header keys and values must be valid UTF-8") from exc
            header_size += len(encoded_key)
            header_size += len(b": ")
            header_size += len(encoded_value)
            header_size += len(b"\r\n")
        return header_size + len(payload)

    def _wire_limit(self, stream: str) -> int:
        connection, _ = self._require_connected()
        limit = int(getattr(connection, "max_payload", 1024 * 1024))
        stream_limit = self._stream_max_msg_sizes.get(stream, -1)
        if stream_limit > 0:
            limit = min(limit, stream_limit)
        return limit

    def _project_first_redrive(self, job: JobEnvelope) -> tuple[DeadLetterRecord, JobEnvelope]:
        prior_attempts = job.metadata.get(ATTEMPT_OFFSET_METADATA, "0")
        try:
            attempt_offset = max(0, int(prior_attempts))
        except ValueError:
            attempt_offset = 0
        projected_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        record = DeadLetterRecord.create(
            failed_at=projected_at,
            disposition=DeadLetterDisposition.EXHAUSTED,
            retryable=True,
            category="unknown",
            classification_reason="payload-size projection",
            attempt=1,
            max_attempts=1,
            cumulative_attempt=attempt_offset + MAX_PROJECTED_SEQUENCE,
            source_stream=self.config.stream,
            source_published_at=projected_at,
            source_consumer=self.config.consumer,
            stream_sequence=1,
            consumer_sequence=1,
            exception_type="builtins.RuntimeError",
            exception_message="payload-size projection",
            traceback="payload-size projection",
            raw_data=job.to_bytes(),
            headers={},
            job=job,
        )
        return record, record.job_for_redrive()

    def _validate_redrive_transaction_size(
        self,
        dead_letter_id: str,
        job: JobEnvelope,
        *,
        redrive_count: int,
        audit_chain: tuple[str, ...],
        initial_submission: bool,
        include_intent: bool,
    ) -> None:
        payload = job.to_bytes()
        projected_audit = RedriveAuditRecord(
            schema_version=REDRIVE_SCHEMA_VERSION,
            dead_letter_id=dead_letter_id,
            redriven_at=MAX_PROJECTED_TIMESTAMP,
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            stream=self.config.stream,
            sequence=MAX_PROJECTED_SEQUENCE,
            redrive_count=redrive_count,
            audit_chain=audit_chain,
        )
        publications: list[tuple[str, bytes, str, dict[Any, str]]] = []
        if include_intent:
            publications.append(
                (
                    "intent",
                    payload,
                    self.config.resolved_dead_letter_stream,
                    self._redrive_intent_headers(
                        dead_letter_id, redrive_count, job
                    ),
                )
            )
        publications.extend(
            (
                (
                    "main-stream job",
                    payload,
                    self.config.stream,
                    self._job_headers(job, f"redrive:{dead_letter_id}"),
                ),
                (
                    "completion audit",
                    projected_audit.to_bytes(),
                    self.config.resolved_dead_letter_stream,
                    self._redrive_audit_headers(
                        dead_letter_id,
                        projected_audit.redrive_count,
                        projected_audit.correlation_id,
                        job.job_type,
                    ),
                ),
            )
        )
        for label, publication_payload, stream, headers in publications:
            limit = self._wire_limit(stream)
            wire_size = self._nats_wire_size(
                publication_payload, headers, stream=stream
            )
            if wire_size > limit:
                message = (
                    f"projected redrive {label} is {wire_size} bytes on the wire; "
                    f"NATS message limit is {limit}"
                )
                if initial_submission:
                    raise JobEnvelopeError(
                        f"job envelope cannot reserve a first redrive: {message}"
                    )
                raise DeadLetterRedriveError(message)

    def _validate_first_redrive_size(self, job: JobEnvelope) -> None:
        record, redriven_job = self._project_first_redrive(job)
        self._validate_redrive_transaction_size(
            record.dead_letter_id,
            redriven_job,
            redrive_count=record.redrive_count + 1,
            audit_chain=record.audit_chain,
            initial_submission=True,
            include_intent=True,
        )

    async def enqueue(
        self, job: JobEnvelope, *, message_id: str | None = None
    ) -> EnqueueReceipt:
        transport_id = job.job_id if message_id is None else message_id
        if not isinstance(transport_id, str) or not transport_id.strip():
            raise ValueError("message_id must be a non-empty string")
        if job.tenant_id is not None and job.tenant_id != "default":
            transport_id = f"tenant:{job.tenant_id}:{transport_id}"
        return await self._publish_job(
            job,
            transport_id=transport_id,
            reserve_redrive_headroom=True,
        )

    async def _publish_job(
        self,
        job: JobEnvelope,
        *,
        transport_id: str,
        reserve_redrive_headroom: bool,
    ) -> EnqueueReceipt:
        parent = job.metadata.get("traceparent") or inject_trace_context().get("traceparent")
        with request_context(correlation_id=job.correlation_id, traceparent=parent):
            with observe("queue", "enqueue", kind="producer"):
                traced_job = replace(job, metadata={**job._wire_metadata(), **inject_trace_context()})
                receipt = await self._publish_job_payload(
                    traced_job,
                    transport_id=transport_id,
                    reserve_redrive_headroom=reserve_redrive_headroom,
                )
                record_job_event("enqueued", operation=_job_operation(job.job_type))
                return receipt

    async def _publish_job_payload(
        self,
        job: JobEnvelope,
        *,
        transport_id: str,
        reserve_redrive_headroom: bool,
    ) -> EnqueueReceipt:
        _, jetstream = self._require_connected()
        payload = job.to_bytes()
        headers = self._job_headers(job, transport_id)
        wire_size = self._nats_wire_size(payload, headers, stream=self.config.stream)
        wire_limit = self._wire_limit(self.config.stream)
        if wire_size > wire_limit:
            raise JobEnvelopeError(
                f"job envelope is {wire_size} bytes on the wire; NATS max_payload "
                f"is {wire_limit}"
            )
        if reserve_redrive_headroom:
            self._validate_first_redrive_size(job)
        try:
            acknowledgement = await jetstream.publish(
                self.config.subject,
                payload,
                stream=self.config.stream,
                headers=headers,
            )
        except APIError as exc:
            reason = (exc.description or "").strip().lower()
            if exc.err_code == STREAM_STORE_FAILED and reason in QUEUE_SATURATION_REASONS:
                raise QueueSaturatedError(self.config.stream, reason) from exc
            raise
        return EnqueueReceipt(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            stream=acknowledgement.stream,
            sequence=acknowledgement.seq,
            duplicate=bool(acknowledgement.duplicate),
        )

    async def claim(self, timeout: float) -> NatsJobDelivery | None:
        _, _, subscription = self._require_consumer()
        try:
            messages = await subscription.fetch(batch=1, timeout=timeout)
        except NatsTimeoutError:
            return None
        message = messages[0]
        return NatsJobDelivery(
            message,
            self.config.request_timeout,
            source_stream=self.config.stream,
            source_consumer=self.config.consumer,
        )

    @staticmethod
    def _canonical_dead_letter_id(dead_letter_id: str) -> str:
        if not isinstance(dead_letter_id, str) or not dead_letter_id.strip():
            raise DeadLetterRecordError("dead_letter_id must be a non-empty UUID")
        try:
            parsed = UUID(dead_letter_id)
        except ValueError as exc:
            raise DeadLetterRecordError("dead_letter_id must be a UUID") from exc
        if str(parsed) != dead_letter_id.lower():
            raise DeadLetterRecordError(
                "dead_letter_id must use canonical UUID format"
            )
        return str(parsed)

    def _dead_letter_entry_subject(self, dead_letter_id: str) -> str:
        return f"{self.config.resolved_dead_letter_subject}.entry.{dead_letter_id}"

    def _dead_letter_chunk_subject(
        self, dead_letter_id: str, record_digest: str, index: int
    ) -> str:
        return (
            f"{self.config.resolved_dead_letter_subject}.chunk."
            f"{dead_letter_id}.{record_digest}.{index:08d}"
        )

    def _redrive_audit_subject(self, dead_letter_id: str) -> str:
        return f"{self.config.resolved_dead_letter_subject}.redrive.{dead_letter_id}"

    def _redrive_intent_subject(self, dead_letter_id: str) -> str:
        return (
            f"{self.config.resolved_dead_letter_subject}.redrive-intent."
            f"{dead_letter_id}"
        )

    @staticmethod
    def _dead_letter_manifest(
        record: DeadLetterRecord, *, record_size: int, record_digest: str, chunks: int
    ) -> bytes:
        return json.dumps(
            {
                "storage_schema_version": DEAD_LETTER_STORAGE_SCHEMA_VERSION,
                "dead_letter_id": record.dead_letter_id,
                "record_size": record_size,
                "record_sha256": record_digest,
                "chunks": chunks,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @staticmethod
    def _parse_dead_letter_manifest(
        data: bytes, *, expected_id: str
    ) -> tuple[int, int, str]:
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise DeadLetterRecordError(
                "dead-letter storage manifest must be UTF-8 JSON"
            ) from exc
        if not isinstance(value, dict):
            raise DeadLetterRecordError("dead-letter storage manifest must be an object")
        expected = {
            "storage_schema_version",
            "dead_letter_id",
            "record_size",
            "record_sha256",
            "chunks",
        }
        if set(value) != expected:
            raise DeadLetterRecordError("dead-letter storage manifest fields are invalid")
        version = value["storage_schema_version"]
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != DEAD_LETTER_STORAGE_SCHEMA_VERSION
        ):
            raise DeadLetterRecordError(
                f"unsupported dead-letter storage schema_version {version}"
            )
        if value["dead_letter_id"] != expected_id:
            raise DeadLetterRecordError(
                "dead-letter storage manifest contains a different record ID"
            )
        record_size = value["record_size"]
        chunks = value["chunks"]
        for field_name, field_value in (("record_size", record_size), ("chunks", chunks)):
            if (
                not isinstance(field_value, int)
                or isinstance(field_value, bool)
                or field_value < 1
            ):
                raise DeadLetterRecordError(
                    f"dead-letter storage {field_name} must be a positive integer"
                )
        if chunks > record_size:
            raise DeadLetterRecordError("dead-letter storage chunk count is invalid")
        digest = value["record_sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise DeadLetterRecordError(
                "dead-letter storage record_sha256 must be lowercase SHA-256"
            )
        return record_size, chunks, digest

    async def _publish_dead_letter_chunk(
        self,
        *,
        dead_letter_id: str,
        record_digest: str,
        index: int,
        payload: bytes,
    ) -> None:
        _, jetstream = self._require_connected()
        subject = self._dead_letter_chunk_subject(
            dead_letter_id, record_digest, index
        )
        headers = self._dead_letter_chunk_headers(
            dead_letter_id, record_digest, index
        )
        wire_size = self._nats_wire_size(
            payload, headers, stream=self.config.resolved_dead_letter_stream
        )
        wire_limit = self._wire_limit(self.config.resolved_dead_letter_stream)
        if wire_size > wire_limit:
            raise DeadLetterRecordError(
                f"dead-letter chunk is {wire_size} bytes on the wire; NATS "
                f"max_payload is {wire_limit}"
            )
        try:
            await jetstream.publish(
                subject,
                payload,
                stream=self.config.resolved_dead_letter_stream,
                headers=headers,
            )
        except APIError as publish_error:
            try:
                existing = await jetstream.get_last_msg(
                    self.config.resolved_dead_letter_stream, subject
                )
            except Exception:
                raise publish_error
            if bytes(existing.data) != payload:
                raise RuntimeError(
                    f"dead-letter chunk collision for {dead_letter_id}"
                ) from publish_error

    @instrument("queue", "dead_letter", kind="producer")
    async def publish_dead_letter(
        self, record: DeadLetterRecord
    ) -> DeadLetterReceipt:
        _, jetstream = self._require_connected()
        await self._ensure_dead_letter_stream()
        subject = self._dead_letter_entry_subject(record.dead_letter_id)
        try:
            existing_entry = await jetstream.get_last_msg(
                self.config.resolved_dead_letter_stream, subject
            )
        except NotFoundError:
            pass
        else:
            existing_record = await self.get_dead_letter(record.dead_letter_id)
            if (
                existing_record.source_stream != record.source_stream
                or existing_record.source_published_at != record.source_published_at
                or existing_record.stream_sequence != record.stream_sequence
                or existing_record.raw_data != record.raw_data
            ):
                raise RuntimeError(
                    f"dead-letter subject collision for {record.dead_letter_id}"
                )
            return DeadLetterReceipt(
                dead_letter_id=record.dead_letter_id,
                stream=self.config.resolved_dead_letter_stream,
                sequence=existing_entry.seq,
                duplicate=True,
            )

        record_payload = record.to_bytes()
        record_digest = hashlib.sha256(record_payload).hexdigest()
        max_payload = self._wire_limit(self.config.resolved_dead_letter_stream)
        projected_chunk_headers = self._dead_letter_chunk_headers(
            record.dead_letter_id, record_digest, MAX_PROJECTED_SEQUENCE
        )
        chunk_header_size = self._nats_wire_size(
            b"",
            projected_chunk_headers,
            stream=self.config.resolved_dead_letter_stream,
        )
        available_chunk_payload = max_payload - chunk_header_size
        if available_chunk_payload < 1:
            raise DeadLetterRecordError(
                "NATS max_payload is too small for dead-letter chunk headers"
            )
        chunk_size = min(DEAD_LETTER_CHUNK_TARGET, available_chunk_payload)
        chunks = [
            record_payload[offset : offset + chunk_size]
            for offset in range(0, len(record_payload), chunk_size)
        ]
        manifest = self._dead_letter_manifest(
            record,
            record_size=len(record_payload),
            record_digest=record_digest,
            chunks=len(chunks),
        )
        headers: dict[Any, str] = {
            Header.MSG_ID: f"dead-letter:{record.dead_letter_id}",
            Header.EXPECTED_LAST_SUBJECT_SEQUENCE: "0",
            DEAD_LETTER_ID_HEADER: record.dead_letter_id,
            DEAD_LETTER_DISPOSITION_HEADER: record.disposition.value,
            DEAD_LETTER_CATEGORY_HEADER: record.category,
        }
        optional_header_values = (
            (record.job.correlation_id, record.job.job_type)
            if record.job is not None
            else ()
        )
        if record.job is not None and all(
            "\r" not in value
            and "\n" not in value
            and _is_utf8_encodable(value)
            for value in optional_header_values
        ):
            headers[CORRELATION_HEADER] = record.job.correlation_id
            headers[JOB_TYPE_HEADER] = record.job.job_type
        manifest_wire_size = self._nats_wire_size(
            manifest, headers, stream=self.config.resolved_dead_letter_stream
        )
        if manifest_wire_size > max_payload and record.job is not None:
            # Full correlation data remains in the checksummed record. Avoid
            # letting unbounded envelope strings make the compact manifest a
            # poison message of its own.
            headers.pop(CORRELATION_HEADER, None)
            headers.pop(JOB_TYPE_HEADER, None)
            manifest_wire_size = self._nats_wire_size(
                manifest, headers, stream=self.config.resolved_dead_letter_stream
            )
        if manifest_wire_size > max_payload:
            raise DeadLetterRecordError(
                "NATS max_payload is too small for a dead-letter storage manifest"
            )
        for index, chunk in enumerate(chunks):
            await self._publish_dead_letter_chunk(
                dead_letter_id=record.dead_letter_id,
                record_digest=record_digest,
                index=index,
                payload=chunk,
            )
        try:
            acknowledgement = await jetstream.publish(
                subject,
                manifest,
                stream=self.config.resolved_dead_letter_stream,
                headers=headers,
            )
        except APIError as publish_error:
            # The immutable per-delivery subject makes DLQ transfer idempotent
            # even after JetStream's finite message-deduplication window.
            try:
                existing_entry = await jetstream.get_last_msg(
                    self.config.resolved_dead_letter_stream, subject
                )
                existing_record = await self.get_dead_letter(record.dead_letter_id)
            except Exception:
                raise publish_error
            if (
                existing_record.dead_letter_id != record.dead_letter_id
                or existing_record.source_stream != record.source_stream
                or existing_record.source_published_at != record.source_published_at
                or existing_record.stream_sequence != record.stream_sequence
                or existing_record.raw_data != record.raw_data
            ):
                raise RuntimeError(
                    f"dead-letter subject collision for {record.dead_letter_id}"
                ) from publish_error
            return DeadLetterReceipt(
                dead_letter_id=record.dead_letter_id,
                stream=self.config.resolved_dead_letter_stream,
                sequence=existing_entry.seq,
                duplicate=True,
            )
        if acknowledgement.duplicate:
            # A dedupe PubAck can outlive a retained message under unsafe server
            # policy. Verify the immutable record before allowing source ACK.
            existing_record = await self.get_dead_letter(record.dead_letter_id)
            if (
                existing_record.source_stream != record.source_stream
                or existing_record.source_published_at != record.source_published_at
                or existing_record.stream_sequence != record.stream_sequence
                or existing_record.raw_data != record.raw_data
            ):
                raise RuntimeError(
                    f"dead-letter subject collision for {record.dead_letter_id}"
                )
        else:
            persisted_record = await self.get_dead_letter(record.dead_letter_id)
            if persisted_record.to_bytes() != record.to_bytes():
                raise RuntimeError(
                    f"dead-letter persistence verification failed for {record.dead_letter_id}"
                )
        return DeadLetterReceipt(
            dead_letter_id=record.dead_letter_id,
            stream=acknowledgement.stream,
            sequence=acknowledgement.seq,
            duplicate=bool(acknowledgement.duplicate),
        )

    async def get_dead_letter(self, dead_letter_id: str) -> DeadLetterRecord:
        identifier = self._canonical_dead_letter_id(dead_letter_id)
        _, jetstream = self._require_connected()
        await self._ensure_dead_letter_stream()
        try:
            message = await jetstream.get_last_msg(
                self.config.resolved_dead_letter_stream,
                self._dead_letter_entry_subject(identifier),
            )
        except NotFoundError as exc:
            raise DeadLetterNotFoundError(
                f"dead-letter {identifier} was not found"
            ) from exc
        record_size, chunk_count, record_digest = self._parse_dead_letter_manifest(
            message.data, expected_id=identifier
        )
        parts: list[bytes] = []
        for index in range(chunk_count):
            try:
                chunk = await jetstream.get_last_msg(
                    self.config.resolved_dead_letter_stream,
                    self._dead_letter_chunk_subject(identifier, record_digest, index),
                )
            except NotFoundError as exc:
                raise DeadLetterRecordError(
                    f"dead-letter {identifier} is missing diagnostic chunk {index}"
                ) from exc
            parts.append(bytes(chunk.data))
        record_payload = b"".join(parts)
        if len(record_payload) != record_size:
            raise DeadLetterRecordError(
                f"dead-letter {identifier} diagnostic size does not match its manifest"
            )
        if hashlib.sha256(record_payload).hexdigest() != record_digest:
            raise DeadLetterRecordError(
                f"dead-letter {identifier} diagnostic checksum does not match its manifest"
            )
        record = DeadLetterRecord.from_bytes(record_payload)
        if record.dead_letter_id != identifier:
            raise DeadLetterRecordError(
                f"dead-letter subject contains record {record.dead_letter_id}"
            )
        return record

    async def _get_redrive_audit(
        self, dead_letter_id: str
    ) -> RedriveAuditRecord | None:
        _, jetstream = self._require_connected()
        try:
            message = await jetstream.get_last_msg(
                self.config.resolved_dead_letter_stream,
                self._redrive_audit_subject(dead_letter_id),
            )
        except NotFoundError:
            return None
        audit = RedriveAuditRecord.from_bytes(message.data)
        if audit.dead_letter_id != dead_letter_id:
            raise DeadLetterRecordError(
                f"redrive subject contains audit for {audit.dead_letter_id}"
            )
        return audit

    async def _get_redrive_intent(
        self, dead_letter_id: str
    ) -> JobEnvelope | None:
        _, jetstream = self._require_connected()
        try:
            message = await jetstream.get_last_msg(
                self.config.resolved_dead_letter_stream,
                self._redrive_intent_subject(dead_letter_id),
            )
        except NotFoundError:
            return None
        try:
            return JobEnvelope.from_bytes(message.data)
        except JobEnvelopeError as exc:
            raise DeadLetterRecordError(
                f"redrive intent for {dead_letter_id} is malformed"
            ) from exc

    @staticmethod
    def _redrive_intent_context(
        dead_letter_id: str, job: JobEnvelope
    ) -> tuple[int, tuple[str, ...]]:
        if job.metadata.get(REDRIVEN_FROM_METADATA) != dead_letter_id:
            raise DeadLetterRecordError(
                f"redrive intent for {dead_letter_id} has the wrong parent"
            )
        encoded_chain = job.metadata.get(DEAD_LETTER_CHAIN_METADATA)
        try:
            chain_value = json.loads(encoded_chain) if encoded_chain is not None else None
        except (ValueError, RecursionError) as exc:
            raise DeadLetterRecordError(
                f"redrive intent for {dead_letter_id} has a malformed audit chain"
            ) from exc
        if not isinstance(chain_value, list) or not chain_value:
            raise DeadLetterRecordError(
                f"redrive intent for {dead_letter_id} has a malformed audit chain"
            )
        audit_chain: list[str] = []
        for value in chain_value:
            if not isinstance(value, str):
                raise DeadLetterRecordError(
                    f"redrive intent for {dead_letter_id} has a malformed audit chain"
                )
            try:
                parsed = UUID(value)
            except ValueError as exc:
                raise DeadLetterRecordError(
                    f"redrive intent for {dead_letter_id} has a malformed audit chain"
                ) from exc
            if str(parsed) != value.lower():
                raise DeadLetterRecordError(
                    f"redrive intent for {dead_letter_id} has a malformed audit chain"
                )
            audit_chain.append(str(parsed))
        if audit_chain[-1] != dead_letter_id:
            raise DeadLetterRecordError(
                f"redrive intent for {dead_letter_id} has the wrong audit chain"
            )

        encoded_count = job.metadata.get(REDRIVE_COUNT_METADATA)
        try:
            redrive_count = int(encoded_count) if encoded_count is not None else 0
        except ValueError as exc:
            raise DeadLetterRecordError(
                f"redrive intent for {dead_letter_id} has a malformed redrive count"
            ) from exc
        if redrive_count < 1 or str(redrive_count) != encoded_count:
            raise DeadLetterRecordError(
                f"redrive intent for {dead_letter_id} has a malformed redrive count"
            )
        return redrive_count, tuple(audit_chain)

    async def _persist_redrive_intent(
        self, record: DeadLetterRecord, job: JobEnvelope
    ) -> JobEnvelope:
        existing = await self._get_redrive_intent(record.dead_letter_id)
        if existing is not None:
            if existing.to_bytes() != job.to_bytes():
                raise RuntimeError(
                    f"redrive intent collision for {record.dead_letter_id}"
                )
            return existing

        _, jetstream = self._require_connected()
        subject = self._redrive_intent_subject(record.dead_letter_id)
        payload = job.to_bytes()
        headers = self._redrive_intent_headers(
            record.dead_letter_id, record.redrive_count + 1, job
        )
        wire_size = self._nats_wire_size(
            payload, headers, stream=self.config.resolved_dead_letter_stream
        )
        wire_limit = self._wire_limit(self.config.resolved_dead_letter_stream)
        if wire_size > wire_limit:
            raise DeadLetterRecordError(
                f"redrive intent is {wire_size} bytes on the wire; NATS max_payload "
                f"is {wire_limit}"
            )
        try:
            acknowledgement = await jetstream.publish(
                subject,
                payload,
                stream=self.config.resolved_dead_letter_stream,
                headers=headers,
            )
        except APIError as publish_error:
            try:
                existing = await self._get_redrive_intent(record.dead_letter_id)
            except Exception:
                raise publish_error
            if existing is None or existing.to_bytes() != job.to_bytes():
                raise RuntimeError(
                    f"redrive intent collision for {record.dead_letter_id}"
                ) from publish_error
            return existing
        if acknowledgement.duplicate:
            existing = await self._get_redrive_intent(record.dead_letter_id)
            if existing is None or existing.to_bytes() != job.to_bytes():
                raise RuntimeError(
                    f"redrive intent was not persisted for {record.dead_letter_id}"
                )
            return existing
        persisted = await self._get_redrive_intent(record.dead_letter_id)
        if persisted is None or persisted.to_bytes() != job.to_bytes():
            raise RuntimeError(
                f"redrive intent was not persisted for {record.dead_letter_id}"
            )
        return persisted

    async def redrive_dead_letter(self, dead_letter_id: str) -> RedriveReceipt:
        identifier = self._canonical_dead_letter_id(dead_letter_id)
        self._require_connected()
        await self._ensure_dead_letter_stream()
        existing_audit = await self._get_redrive_audit(identifier)
        if existing_audit is not None:
            return existing_audit.to_receipt(duplicate=True)

        job = await self._get_redrive_intent(identifier)
        pending_record: DeadLetterRecord | None = None
        if job is None:
            record = await self.get_dead_letter(identifier)
            job = record.job_for_redrive()
            pending_record = record
        with request_context(
            correlation_id=job.correlation_id,
            traceparent=job.metadata.get("traceparent"),
        ):
            with observe("queue", "redrive", kind="producer"):
                return await self._redrive_job(identifier, job, pending_record)

    async def _redrive_job(
        self,
        identifier: str,
        job: JobEnvelope,
        pending_record: DeadLetterRecord | None,
    ) -> RedriveReceipt:
        redrive_count, audit_chain = self._redrive_intent_context(identifier, job)
        self._validate_redrive_transaction_size(
            identifier,
            job,
            redrive_count=redrive_count,
            audit_chain=audit_chain,
            initial_submission=False,
            include_intent=pending_record is not None,
        )
        if pending_record is not None:
            job = await self._persist_redrive_intent(pending_record, job)
        # The intent remains immutable across retrying the redrive transaction.
        # Only the transport copy descends from this specific redrive attempt.
        transport_job = replace(job, metadata={**job._wire_metadata(), **inject_trace_context()})
        enqueue_receipt = await self._publish_job(
            transport_job,
            transport_id=f"redrive:{identifier}",
            reserve_redrive_headroom=False,
        )
        record_job_event("redriven", operation=_job_operation(job.job_type))
        audit = RedriveAuditRecord(
            schema_version=REDRIVE_SCHEMA_VERSION,
            dead_letter_id=identifier,
            redriven_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            stream=enqueue_receipt.stream,
            sequence=enqueue_receipt.sequence,
            redrive_count=redrive_count,
            audit_chain=audit_chain,
        )
        _, jetstream = self._require_connected()
        subject = self._redrive_audit_subject(identifier)
        payload = audit.to_bytes()
        headers = self._redrive_audit_headers(
            identifier, audit.redrive_count, audit.correlation_id, job.job_type
        )
        wire_size = self._nats_wire_size(
            payload, headers, stream=self.config.resolved_dead_letter_stream
        )
        wire_limit = self._wire_limit(self.config.resolved_dead_letter_stream)
        if wire_size > wire_limit:
            raise DeadLetterRecordError(
                f"redrive completion audit is {wire_size} bytes on the wire; NATS "
                f"max_payload is {wire_limit}"
            )
        try:
            await jetstream.publish(
                subject,
                payload,
                stream=self.config.resolved_dead_letter_stream,
                headers=headers,
            )
        except APIError as publish_error:
            try:
                existing_audit = await self._get_redrive_audit(identifier)
            except Exception:
                raise publish_error
            if existing_audit is None:
                raise publish_error
            if (
                existing_audit.job_id != audit.job_id
                or existing_audit.audit_chain != audit.audit_chain
            ):
                raise RuntimeError(
                    f"redrive audit subject collision for {identifier}"
                ) from publish_error
            return existing_audit.to_receipt(duplicate=True)
        return audit.to_receipt(duplicate=enqueue_receipt.duplicate)

    def _bus_state(self) -> BusState:
        connection = self._connection
        if connection is None:
            return BusState.DISCONNECTED
        if connection.is_closed:
            return BusState.CLOSED
        if connection.is_draining:
            return BusState.DRAINING
        if connection.is_reconnecting:
            return BusState.RECONNECTING
        if connection.is_connected:
            return BusState.CONNECTED
        return BusState.DISCONNECTED

    async def probe(self) -> QueueHealth:
        state = self._bus_state()
        if state != BusState.CONNECTED or self._jetstream is None:
            return QueueHealth(
                state=state,
                ready=False,
                jetstream=False,
                stream=self.config.stream,
                consumer=self.config.consumer,
                max_messages=self.config.stream_max_messages,
                max_bytes=self.config.stream_max_bytes,
                error="NATS is not connected",
            )
        try:
            assert self._connection is not None
            await self._connection.flush(timeout=self.config.request_timeout)
            await self._jetstream.account_info()
            stream_info = await self._jetstream.stream_info(self.config.stream)
            consumer = None
            if self._consume:
                consumer = await self._jetstream.consumer_info(
                    self.config.stream, self.config.consumer
                )
        except Exception:
            return QueueHealth(
                state=self._bus_state(),
                ready=False,
                jetstream=False,
                stream=self.config.stream,
                consumer=self.config.consumer,
                max_messages=self.config.stream_max_messages,
                max_bytes=self.config.stream_max_bytes,
                error="NATS readiness probe failed",
            )
        stream_config = stream_info.config
        max_messages = getattr(stream_config, "max_msgs", None)
        max_bytes = getattr(stream_config, "max_bytes", None)
        if not isinstance(max_messages, int) or isinstance(max_messages, bool):
            max_messages = None
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool):
            max_bytes = None

        topology_error: str | None = None
        if not self._main_stream_config_compatible(stream_config):
            topology_error = (
                f"NATS stream {self.config.stream!r} has incompatible configuration"
            )
        elif consumer is not None and not self._consumer_config_compatible(
            consumer.config
        ):
            topology_error = (
                f"NATS consumer {self.config.consumer!r} has incompatible configuration"
            )

        stored_messages = getattr(stream_info.state, "messages", None)
        stored_bytes = getattr(stream_info.state, "bytes", None)
        utilization: float | None = None
        saturated: bool | None = None
        ratios: list[float] = []
        if isinstance(stored_messages, int) and not isinstance(stored_messages, bool):
            if max_messages is not None and max_messages > 0:
                ratios.append(stored_messages / max_messages)
        if isinstance(stored_bytes, int) and not isinstance(stored_bytes, bool):
            if max_bytes is not None and max_bytes > 0:
                ratios.append(stored_bytes / max_bytes)
        if ratios:
            utilization = max(ratios)
            saturated = utilization >= 1.0
        bus_state = self._bus_state()
        return QueueHealth(
            state=bus_state,
            ready=bus_state == BusState.CONNECTED and topology_error is None,
            jetstream=True,
            stream=self.config.stream,
            consumer=self.config.consumer,
            pending=consumer.num_pending if consumer is not None else None,
            ack_pending=consumer.num_ack_pending if consumer is not None else None,
            redelivered=consumer.num_redelivered if consumer is not None else None,
            stored_messages=stored_messages,
            stored_bytes=stored_bytes,
            max_messages=max_messages,
            max_bytes=max_bytes,
            utilization=utilization,
            saturated=saturated,
            error=topology_error,
        )

    async def close(self, *, graceful: bool = True) -> None:
        connection = self._connection
        subscription = self._subscription
        self._subscription = None
        self._jetstream = None
        self._connection = None
        self._dead_letter_ready = False
        self._stream_max_msg_sizes.clear()
        if connection is None or connection.is_closed:
            return
        if graceful:
            if subscription is not None:
                await subscription.unsubscribe()
            await connection.drain()
        else:
            await connection.close()
