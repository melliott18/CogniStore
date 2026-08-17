"""Durable background jobs for CogniStore."""

from .models import (
    DeadLetterDisposition,
    DeadLetterNotFoundError,
    DeadLetterReceipt,
    DeadLetterRecord,
    DeadLetterRecordError,
    DeadLetterRedriveError,
    EnqueueReceipt,
    InvalidJobError,
    JobContext,
    JobEnvelope,
    JobEnvelopeError,
    QueueHealth,
    RedriveReceipt,
)
from .nats_queue import NatsJetStreamConfig, NatsJetStreamQueue
from .retry import (
    ErrorClassification,
    FailureCategory,
    RetryableJobError,
    RetryPolicy,
    TerminalJobError,
    classify_job_error,
)
from .runtime import AsyncWorker, WorkerConfig, WorkerSnapshot, WorkerState

__all__ = [
    "AsyncWorker",
    "DeadLetterDisposition",
    "DeadLetterNotFoundError",
    "DeadLetterReceipt",
    "DeadLetterRecord",
    "DeadLetterRecordError",
    "DeadLetterRedriveError",
    "EnqueueReceipt",
    "ErrorClassification",
    "FailureCategory",
    "JobContext",
    "JobEnvelope",
    "JobEnvelopeError",
    "InvalidJobError",
    "NatsJetStreamConfig",
    "NatsJetStreamQueue",
    "QueueHealth",
    "RedriveReceipt",
    "RetryPolicy",
    "RetryableJobError",
    "TerminalJobError",
    "WorkerConfig",
    "WorkerSnapshot",
    "WorkerState",
    "classify_job_error",
]
