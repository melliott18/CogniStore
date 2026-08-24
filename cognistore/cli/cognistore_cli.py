from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import signal
import sys
import time
import traceback
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any, Literal, NoReturn, Sequence
from uuid import uuid4

from cognistore.cli.config import (
	CliConfigError,
	CliConfigResolution,
	json_requested,
	resolve_cli_config,
)
from cognistore.cli.output import (
	VerboseReporter,
	emit_json,
	error_payload,
	redact,
	redact_text,
	result_payload,
)
from cognistore.core.catalog import Catalog
from cognistore.core.move_jobs import MoveJob, MoveJobState, MoveJobTransition
from cognistore.core.mover import Mover
from cognistore.core.policy_factory import build_policy
from cognistore.core.policy_runner import ActionResult, PolicyRunner
from cognistore.core.scanner import scan_catalog
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.core.throughput import (
	DEFAULT_MAX_QUEUE_DEPTH,
	ThroughputConfig,
	ThroughputController,
	TierLimits,
	load_throughput_config,
)
from cognistore.drivers.driver_loader import load_drivers
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.storage_driver import StorageDriver
from cognistore.jobs.handlers import (
	CATALOG_SCAN_JOB,
	POLICY_RUN_JOB,
	build_handlers,
	policy_job_payload,
)
from cognistore.jobs.health import HealthServer
from cognistore.jobs.models import JobEnvelope
from cognistore.jobs.nats_queue import (
	DEFAULT_DEAD_LETTER_MAX_AGE,
	DEFAULT_STREAM_MAX_BYTES,
	DEFAULT_STREAM_MAX_MESSAGES,
	NatsJetStreamConfig,
	NatsJetStreamQueue,
)
from cognistore.jobs.runtime import AsyncWorker, WorkerConfig, WorkerState
from cognistore.jobs.scheduler import (
	PeriodicScheduler,
	ScheduledRunCoordinator,
	SQLiteScheduleStore,
	load_schedule_config,
)
from cognistore.utils.device_info import (
	discover_device_for_tier,
	load_hardware_json,
	save_hardware_json,
)
from cognistore.utils.drive_profiles import DEFAULT_PROFILES
from cognistore.utils.tier_profiler import load_metrics_json, profile_path, save_metrics_json

LOGGER = logging.getLogger(__name__)

_COMMAND_ALIASES = {
	"job-redrive": "dead-letter-redrive",
	"dlq-redrive": "dead-letter-redrive",
}
_COMMAND_NAMES = frozenset(
	{
		"put",
		"get",
		"ls",
		"move",
		"move-status",
		"move-list",
		"move-resume",
		"ls-tier",
		"catalog-scan",
		"tier-profile",
		"devices-scan",
		"auto-refresh",
		"policy-run",
		"worker",
		"scheduler",
		"dead-letter-redrive",
		"job-redrive",
		"dlq-redrive",
	}
)
_CLI_VALUE_OPTIONS = {
	"--base": "base",
	"--drivers": "drivers",
	"--catalog-db": "catalog_db",
	"--nats-url": "nats_url",
	"--job-stream": "job_stream",
	"--job-subject": "job_subject",
	"--job-consumer": "job_consumer",
	"--ack-wait": "ack_wait",
	"--stream-max-messages": "stream_max_messages",
	"--stream-max-bytes": "stream_max_bytes",
	"--dead-letter-stream": "dead_letter_stream",
	"--dead-letter-subject": "dead_letter_subject",
	"--dead-letter-max-age": "dead_letter_max_age",
	"--json": "json",
	"--no-json": "json",
	"--dry-run": "dry_run",
	"--no-dry-run": "dry_run",
	"--verbose": "verbose",
}


def _canonical_command(command: str | None) -> str | None:
	if command is None:
		return None
	return _COMMAND_ALIASES.get(command, command)


def _command_hint(argv: Sequence[str]) -> str | None:
	for token in argv:
		if token in _COMMAND_NAMES:
			return _canonical_command(token)
	return None


def _command_line_value_sources(argv: Sequence[str]) -> set[str]:
	"""Return global value names explicitly selected on the command line."""

	sources: set[str] = set()
	for argument in argv:
		if argument == "--":
			break
		option = argument.partition("=")[0]
		if option in _CLI_VALUE_OPTIONS:
			sources.add(_CLI_VALUE_OPTIONS[option])
		elif argument.startswith("-v") and set(argument[1:]) == {"v"}:
			sources.add("verbose")
	return sources


class _CliArgumentParser(argparse.ArgumentParser):
	"""Argument parser that preserves the JSON-only stdout contract on errors."""

	json_output = False
	command_hint: str | None = None

	def print_help(self, file: Any | None = None) -> None:
		if type(self).json_output:
			emit_json(
				result_payload(
					type(self).command_hint,
					"success",
					help_format="text",
					help=self.format_help(),
				)
			)
			return
		super().print_help(file)

	def error(self, message: str) -> NoReturn:
		safe_message = redact_text(message)
		if type(self).json_output:
			emit_json(
				error_payload(
					type(self).command_hint,
					safe_message,
					exit_code=2,
					error_type="UsageError",
				)
			)
			raise SystemExit(2)
		super().error(safe_message)


class _RedactingLogFilter(logging.Filter):
	"""Redact messages and tracebacks emitted by CLI-reachable components."""

	def filter(self, record: logging.LogRecord) -> bool:
		try:
			message = record.getMessage()
		except Exception:
			message = str(record.msg)
		record.msg = redact_text(message)
		record.args = ()
		if record.exc_info is not None:
			record.exc_text = redact_text(
				"".join(traceback.format_exception(*record.exc_info)).rstrip()
			)
			record.exc_info = None
		if record.stack_info is not None:
			record.stack_info = redact_text(record.stack_info)
		return True


@contextmanager
def _cli_logging(verbose: bool) -> Iterator[None]:
	"""Route CogniStore logs through one redacting stderr handler."""

	logger = logging.getLogger("cognistore")
	previous_level = logger.level
	previous_propagate = logger.propagate
	handler = logging.StreamHandler(sys.stderr)
	handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
	handler.addFilter(_RedactingLogFilter())
	handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
	logger.setLevel(logging.DEBUG if verbose else logging.WARNING)
	logger.propagate = False
	logger.addHandler(handler)
	try:
		yield
	finally:
		logger.removeHandler(handler)
		handler.close()
		logger.setLevel(previous_level)
		logger.propagate = previous_propagate


class _AppendOverrideDefault(argparse.Action):
	"""Make the first explicit append replace configured/default values."""

	def __call__(
		self,
		parser: argparse.ArgumentParser,
		namespace: argparse.Namespace,
		values: str | Sequence[Any] | None,
		option_string: str | None = None,
	) -> None:
		marker = f"_cli_seen_{self.dest}"
		if not isinstance(values, str):
			parser.error(f"{option_string or self.dest} requires one URL")
		current = list(getattr(namespace, self.dest, ()) or ())
		if not getattr(namespace, marker, False):
			current = []
			setattr(namespace, marker, True)
		current.append(values)
		setattr(namespace, self.dest, current)


def _add_output_options(
	parser: argparse.ArgumentParser, *, subcommand: bool = False
) -> None:
	default: Any = argparse.SUPPRESS if subcommand else False
	parser.add_argument(
		"--json",
		action=argparse.BooleanOptionalAction,
		default=default,
		help="Emit versioned JSON output on stdout",
	)
	parser.add_argument(
		"--dry-run",
		action=argparse.BooleanOptionalAction,
		default=default,
		help="Validate and plan mutating work without writing",
	)
	parser.add_argument(
		"-v",
		"--verbose",
		action="count",
		default=argparse.SUPPRESS if subcommand else 0,
		help="Emit redacted diagnostics to stderr (repeat for more detail)",
	)


def _emit_result(
	command: str,
	status: str,
	*,
	json_output: bool,
	human: str | Sequence[str] | None = None,
	**fields: object,
) -> None:
	if json_output:
		emit_json(redact(result_payload(command, status, **fields)))
		return
	if human is None:
		return
	lines = (human,) if isinstance(human, str) else human
	for line in lines:
		print(redact_text(line))


def _emit_failure(
	command: str | None,
	error: BaseException | str,
	*,
	json_output: bool,
	exit_code: int = 1,
	error_type: str | None = None,
	retryable: bool = False,
	**fields: object,
) -> int:
	payload = {
		**error_payload(
			command,
			error,
			exit_code=exit_code,
			retryable=retryable,
			error_type=error_type,
		),
		**fields,
	}
	if json_output:
		emit_json(redact(payload))
	else:
		message = str(error) if isinstance(error, BaseException) else error
		print(f"error: {redact_text(message)}", file=sys.stderr)
	return exit_code


def _move_job_payload(job: MoveJob) -> dict[str, object]:
	return {
		"idempotency_key": job.idempotency_key,
		"state": job.state.value,
		"src_tier": job.src_tier,
		"dst_tier": job.dst_tier,
		"bucket": job.bucket,
		"key": job.key,
		"expected_size": job.expected_size,
		"owner_id": job.owner_id,
		"lease_expires_at": job.lease_expires_at,
		"transferred_size": job.transferred_size,
		"source_size": job.source_size,
		"source_checksum": job.source_checksum,
		"destination_size": job.destination_size,
		"destination_checksum": job.destination_checksum,
		"destination_generation": job.destination_generation,
		"source_generation": job.source_metadata.get("generation"),
		"verification_details": list(job.verification_details),
		"terminal_reason": job.terminal_reason,
		"terminal": job.state.terminal,
		"created_at": job.created_at,
		"updated_at": job.updated_at,
	}


def _move_transition_payload(transition: MoveJobTransition) -> dict[str, object]:
	return {
		"sequence": transition.sequence,
		"idempotency_key": transition.idempotency_key,
		"from_state": (
			None if transition.from_state is None else transition.from_state.value
		),
		"to_state": transition.to_state.value,
		"reason": redact_text(transition.reason),
		"created_at": transition.created_at,
	}


def _queue_config(
	args: argparse.Namespace, *, client_name: str, one_shot: bool = False
) -> NatsJetStreamConfig:
	servers = tuple(args.nats_url or [os.environ.get("COGNISTORE_NATS_URL", "nats://127.0.0.1:4222")])
	return NatsJetStreamConfig(
		servers=servers,
		stream=args.job_stream,
		subject=args.job_subject,
		consumer=args.job_consumer,
		ack_wait=args.ack_wait,
		max_ack_pending=getattr(args, "max_ack_pending", 1),
		stream_max_messages=getattr(
			args, "stream_max_messages", DEFAULT_STREAM_MAX_MESSAGES
		),
		stream_max_bytes=getattr(
			args, "stream_max_bytes", DEFAULT_STREAM_MAX_BYTES
		),
		client_name=client_name,
		max_reconnect_attempts=1 if one_shot else 60,
		allow_reconnect=not one_shot,
		reconnect_time_wait=0.1 if one_shot else 2.0,
		report_connection_errors=not one_shot,
		dead_letter_stream=getattr(args, "dead_letter_stream", None),
		dead_letter_subject=getattr(args, "dead_letter_subject", None),
		dead_letter_max_age=getattr(
			args, "dead_letter_max_age", DEFAULT_DEAD_LETTER_MAX_AGE
		),
	)


def _throughput_config(
	args: argparse.Namespace, drivers: dict[str, StorageDriver]
) -> ThroughputConfig:
	config_path = getattr(args, "tier_limits", None)
	if config_path:
		return load_throughput_config(config_path, known_tiers=drivers)
	return ThroughputConfig(
		max_queue_depth=DEFAULT_MAX_QUEUE_DEPTH,
		tiers={tier: TierLimits() for tier in drivers},
	)


async def _enqueue_job(
	config: NatsJetStreamConfig, job: JobEnvelope
):
	queue = NatsJetStreamQueue(config, consume=False)
	try:
		await queue.connect()
		return await queue.enqueue(job)
	finally:
		# A PubAck means the job is already durable. Producer cleanup must not
		# replace that success (or the original publish error) with a close error,
		# since reporting an ambiguous failure encourages a duplicate retry.
		try:
			await queue.close(graceful=False)
		except Exception as exc:
			LOGGER.warning(
				"failed to close NATS publisher: %s", redact_text(str(exc))
			)


async def _redrive_dead_letter(config: NatsJetStreamConfig, dead_letter_id: str):
	dead_letter_id = NatsJetStreamQueue._canonical_dead_letter_id(dead_letter_id)
	queue = NatsJetStreamQueue(config, consume=False)
	try:
		await queue.connect()
		return await queue.redrive_dead_letter(dead_letter_id)
	finally:
		try:
			await queue.close(graceful=False)
		except Exception as exc:
			LOGGER.warning(
				"failed to close NATS redrive connection: %s",
				redact_text(str(exc)),
			)


def _render_enqueue(job: JobEnvelope, receipt, *, json_output: bool) -> None:
	payload = {
		"status": "queued",
		"job_id": receipt.job_id,
		"correlation_id": receipt.correlation_id,
		"job_type": job.job_type,
		"stream": receipt.stream,
		"sequence": receipt.sequence,
		"duplicate": receipt.duplicate,
	}
	_emit_result(
		"catalog-scan" if job.job_type == CATALOG_SCAN_JOB else "policy-run",
		"queued",
		json_output=json_output,
		human=(
			f"queued {job.job_type} job_id={job.job_id} "
			f"correlation_id={job.correlation_id}"
		),
		**{key: value for key, value in payload.items() if key != "status"},
	)


def _render_enqueue_error(
	job: JobEnvelope, exc: Exception, *, json_output: bool
) -> None:
	payload = {
		"operation": "enqueue",
		"job_id": job.job_id,
		"correlation_id": job.correlation_id,
		"job_type": job.job_type,
		"error_type": type(exc).__name__,
		"error": str(exc),
		"retryable": bool(getattr(exc, "retryable", False)),
	}
	if json_output:
		emit_json(
			redact(
				{
					**error_payload(
						"catalog-scan"
						if job.job_type == CATALOG_SCAN_JOB
						else "policy-run",
						exc,
						retryable=bool(payload["retryable"]),
					),
					**payload,
				}
			)
		)
	else:
		print(f"enqueue failed: {redact_text(str(exc))}", file=sys.stderr)


def _submit_job(args: argparse.Namespace, job: JobEnvelope) -> int:
	try:
		receipt = asyncio.run(
			_enqueue_job(
				_queue_config(args, client_name="cognistore-cli", one_shot=True),
				job,
			)
		)
	except Exception as exc:
		_render_enqueue_error(job, exc, json_output=args.json)
		return 1
	_render_enqueue(job, receipt, json_output=args.json)
	return 0


def _render_redrive(receipt, *, json_output: bool) -> None:
	payload = {
		"status": "redriven",
		"dead_letter_id": receipt.dead_letter_id,
		"job_id": receipt.job_id,
		"correlation_id": receipt.correlation_id,
		"stream": receipt.stream,
		"sequence": receipt.sequence,
		"redrive_count": receipt.redrive_count,
		"audit_chain": list(receipt.audit_chain),
		"duplicate": receipt.duplicate,
	}
	verb = "already redriven" if receipt.duplicate else "redriven"
	_emit_result(
		"dead-letter-redrive",
		"redriven",
		json_output=json_output,
		human=(
			f"{verb} job_id={receipt.job_id} "
			f"dead_letter_id={receipt.dead_letter_id} "
			f"redrive_count={receipt.redrive_count}"
		),
		**{key: value for key, value in payload.items() if key != "status"},
	)


def _render_redrive_error(exc: Exception, *, json_output: bool) -> None:
	payload = {
		"status": "error",
		"operation": "dead-letter-redrive",
		"error_type": type(exc).__name__,
		"error": str(exc),
	}
	if json_output:
		emit_json(
			redact(
				{
					**error_payload("dead-letter-redrive", exc),
					**{key: value for key, value in payload.items() if key != "status"},
				}
			)
		)
	else:
		print(f"redrive failed: {redact_text(str(exc))}", file=sys.stderr)


async def _serve_worker(
	args: argparse.Namespace, drivers, catalog: SQLiteCatalog
) -> int:
	throughput = (
		ThroughputController(
			getattr(args, "_throughput_config", None)
			or _throughput_config(args, drivers)
		)
		if drivers
		else None
	)
	queue = NatsJetStreamQueue(_queue_config(args, client_name="cognistore-worker"))
	schedule_db = getattr(args, "catalog_db", None) or getattr(
		catalog, "db_path", ":memory:"
	)
	if not isinstance(schedule_db, (str, Path)):
		schedule_db = ":memory:"
	schedule_store = SQLiteScheduleStore(schedule_db)
	worker = AsyncWorker(
		queue,
		build_handlers(drivers, catalog, throughput=throughput),
		config=WorkerConfig(
			fetch_timeout=args.fetch_timeout,
			heartbeat_interval=args.heartbeat_interval,
			shutdown_grace=args.shutdown_grace,
			settlement_timeout=args.settlement_timeout,
			stop_after_jobs=1 if args.once else None,
			max_attempts=getattr(args, "max_attempts", 7),
			retry_base_delay=getattr(args, "retry_base_delay", 1.0),
			retry_max_delay=getattr(args, "retry_max_delay", 30.0),
			retry_jitter=getattr(args, "retry_jitter", 0.2),
			max_in_flight=getattr(args, "max_in_flight", 1),
		),
		throughput=throughput,
		coordinator=ScheduledRunCoordinator(
			schedule_store, lease_seconds=getattr(args, "schedule_lock_ttl", 60.0)
		),
	)
	health = HealthServer(worker, host=args.health_host, port=args.health_port)
	loop = asyncio.get_running_loop()
	reload_executor = (
		ThreadPoolExecutor(max_workers=1, thread_name_prefix="cognistore-limit-reload")
		if throughput is not None and getattr(args, "tier_limits", None)
		else None
	)
	installed_signals: list[signal.Signals] = []
	reload_task: asyncio.Task[None] | None = None
	reload_generation = 0
	reload_processed = 0

	async def reload_limits() -> None:
		nonlocal reload_processed
		assert throughput is not None
		assert reload_executor is not None
		while reload_processed < reload_generation:
			target_generation = reload_generation
			try:
				# Limit reloads must not queue behind move operations in asyncio's
				# default executor: raising a low byte rate is itself a recovery
				# mechanism for a saturated worker.
				config = await loop.run_in_executor(
					reload_executor,
					partial(
						load_throughput_config,
						args.tier_limits,
						known_tiers=drivers,
					),
				)
				await throughput.reconfigure(config)
			except Exception as exc:
				LOGGER.error(
					"tier-limit reload failed; retaining the previous configuration: %s",
					redact_text(str(exc)),
				)
			else:
				LOGGER.info(
					"reloaded tier throughput limits from %s", args.tier_limits
				)
			finally:
				reload_processed = target_generation

	def request_limit_reload() -> None:
		nonlocal reload_generation, reload_task
		reload_generation += 1
		if reload_task is None or reload_task.done():
			reload_task = asyncio.create_task(
				reload_limits(), name="cognistore-tier-limit-reload"
			)

	try:
		await worker.start()
		await health.start()
		for signum in (signal.SIGINT, signal.SIGTERM):
			try:
				loop.add_signal_handler(signum, worker.request_shutdown)
				installed_signals.append(signum)
			except (NotImplementedError, RuntimeError):
				pass
		if throughput is not None and getattr(args, "tier_limits", None):
			try:
				loop.add_signal_handler(signal.SIGHUP, request_limit_reload)
				installed_signals.append(signal.SIGHUP)
			except (AttributeError, NotImplementedError, RuntimeError):
				pass
		_emit_result(
			"worker",
			"ready",
			json_output=bool(getattr(args, "json", False)),
			human=(
				f"worker ready health=http://{args.health_host}:{health.bound_port} "
				f"stream={args.job_stream} consumer={args.job_consumer}"
			),
			health_url=f"http://{args.health_host}:{health.bound_port}",
			stream=args.job_stream,
			consumer=args.job_consumer,
		)
		sys.stdout.flush()
		await worker.wait_for_shutdown_request()
		report = await worker.shutdown()
		return 0 if report.graceful and worker.state == WorkerState.STOPPED else 1
	finally:
		for signum in installed_signals:
			loop.remove_signal_handler(signum)
		if reload_task is not None:
			await asyncio.gather(reload_task, return_exceptions=True)
		if reload_executor is not None:
			reload_executor.shutdown(wait=True, cancel_futures=True)
		if worker.state not in (WorkerState.STOPPED, WorkerState.FAILED):
			await worker.shutdown()
		await health.close()
		schedule_store.close()


async def _serve_scheduler(args: argparse.Namespace, schedules) -> int:
	store = SQLiteScheduleStore(args.catalog_db)
	queue = NatsJetStreamQueue(
		_queue_config(args, client_name="cognistore-scheduler"), consume=False
	)
	scheduler = PeriodicScheduler(queue, store, schedules)
	stop_requested = asyncio.Event()
	loop = asyncio.get_running_loop()
	installed_signals: list[signal.Signals] = []

	try:
		await scheduler.start()
		for signum in (signal.SIGINT, signal.SIGTERM):
			try:
				loop.add_signal_handler(signum, stop_requested.set)
				installed_signals.append(signum)
			except (NotImplementedError, RuntimeError):
				pass
		_emit_result(
			"scheduler",
			"ready",
			json_output=bool(getattr(args, "json", False)),
			human=f"scheduler ready schedules={len(schedules)} stream={args.job_stream}",
			count=len(schedules),
			stream=args.job_stream,
		)
		sys.stdout.flush()
		if args.once:
			await scheduler.run_due()
			return 0

		while not stop_requested.is_set():
			try:
				await scheduler.run_due()
			except Exception as exc:
				LOGGER.error(
					"scheduler publication cycle completed with one or more errors: %s",
					redact_text(str(exc)),
				)
			try:
				await asyncio.wait_for(
					stop_requested.wait(), timeout=args.poll_interval
				)
			except asyncio.TimeoutError:
				pass
		return 0
	finally:
		for signum in installed_signals:
			loop.remove_signal_handler(signum)
		await scheduler.close()
		store.close()


def _render_actions(
	actions: list[ActionResult],
	*,
	command: str,
	dry_run: bool,
	json_output: bool,
	extra: dict[str, object] | None = None,
) -> None:
	action_payloads = [
		{
			"status": action.status,
			"bucket": action.bucket,
			"key": action.key,
			"from_tier": action.from_tier,
			"to_tier": action.to_tier,
			"reason": redact_text(action.reason),
		}
		for action in actions
	]
	human = [
		(
			f"{'planned' if action.status == 'planned' else 'moved'} "
			f"{action.bucket}/{action.key} "
			f"{action.from_tier}->{action.to_tier} : {redact_text(action.reason)}"
		)
		for action in actions
	]
	summary = "planned_actions" if dry_run else "completed_actions"
	human.append(f"{summary}={len(actions)}")
	_emit_result(
		command,
		"planned" if dry_run else "completed",
		json_output=json_output,
		human=human,
		dry_run=dry_run,
		count=len(actions),
		actions=action_payloads,
		**(extra or {}),
	)


def _run_cli(
	argv: Sequence[str],
	resolution: CliConfigResolution,
	catalog_stack: ExitStack,
) -> int:
	def open_sqlite_catalog(
		path: str | Path,
		*,
		read_only: bool = False,
	) -> SQLiteCatalog:
		opened = SQLiteCatalog(path, read_only=read_only)
		catalog_stack.callback(opened.close)
		return opened

	parser = _CliArgumentParser(prog="cognistore", description="CogniStore CLI")
	parser.add_argument(
		"--config",
		help="Path to the CogniStore CLI configuration file",
	)
	parser.add_argument(
		"--no-config",
		action="store_true",
		help="Disable loading the implicit or environment-selected config file",
	)
	parser.add_argument("--profile", help="Named CogniStore configuration profile")
	_add_output_options(parser)
	parser.add_argument("--base", help="Base path for POSIX storage (used when --drivers is not provided)")
	parser.add_argument("--drivers", help="Path to drivers.yaml to enable multi-tier operations")
	parser.add_argument("--catalog-db", help="Path to SQLite catalog DB; if omitted uses in-memory catalog")
	parser.add_argument(
		"--nats-url",
		action=_AppendOverrideDefault,
		help="NATS server URL (repeat for a cluster; defaults to COGNISTORE_NATS_URL)",
	)
	parser.add_argument("--job-stream", default="COGNISTORE_JOBS")
	parser.add_argument("--job-subject", default="cognistore.jobs")
	parser.add_argument("--job-consumer", default="cognistore-workers")
	parser.add_argument("--ack-wait", type=float, default=30.0, help="Seconds before an unacknowledged job is redelivered")
	parser.add_argument(
		"--stream-max-messages",
		type=int,
		default=DEFAULT_STREAM_MAX_MESSAGES,
		help="Maximum durable jobs retained by the work stream",
	)
	parser.add_argument(
		"--stream-max-bytes",
		type=int,
		default=DEFAULT_STREAM_MAX_BYTES,
		help="Maximum bytes retained by the work stream",
	)
	parser.add_argument(
		"--dead-letter-stream",
		help="Dead-letter stream (defaults to <job-stream>_DLQ)",
	)
	parser.add_argument(
		"--dead-letter-subject",
		help="Dead-letter subject prefix (defaults to <job-subject>.dead)",
	)
	parser.add_argument(
		"--dead-letter-max-age",
		type=float,
		default=DEFAULT_DEAD_LETTER_MAX_AGE,
		help="Seconds to retain immutable dead-letter diagnostics",
	)
	sub = parser.add_subparsers(dest="cmd", required=True)

	def command(name: str, **kwargs: Any) -> argparse.ArgumentParser:
		command_parser = sub.add_parser(name, **kwargs)
		command_parser.add_argument(
			"--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS
		)
		command_parser.add_argument(
			"--no-config",
			action="store_true",
			default=argparse.SUPPRESS,
			help=argparse.SUPPRESS,
		)
		command_parser.add_argument(
			"--profile", default=argparse.SUPPRESS, help=argparse.SUPPRESS
		)
		_add_output_options(command_parser, subcommand=True)
		command_parser.add_argument(
			"--base", default=argparse.SUPPRESS, help=argparse.SUPPRESS
		)
		command_parser.add_argument(
			"--drivers", default=argparse.SUPPRESS, help=argparse.SUPPRESS
		)
		command_parser.add_argument(
			"--catalog-db", default=argparse.SUPPRESS, help=argparse.SUPPRESS
		)
		command_parser.add_argument(
			"--nats-url",
			action=_AppendOverrideDefault,
			default=argparse.SUPPRESS,
			help=argparse.SUPPRESS,
		)
		for option, destination in (
			("--job-stream", "job_stream"),
			("--job-subject", "job_subject"),
			("--job-consumer", "job_consumer"),
			("--dead-letter-stream", "dead_letter_stream"),
			("--dead-letter-subject", "dead_letter_subject"),
		):
			command_parser.add_argument(
				option,
				dest=destination,
				default=argparse.SUPPRESS,
				help=argparse.SUPPRESS,
			)
		for option, destination, value_type in (
			("--ack-wait", "ack_wait", float),
			("--stream-max-messages", "stream_max_messages", int),
			("--stream-max-bytes", "stream_max_bytes", int),
			("--dead-letter-max-age", "dead_letter_max_age", float),
		):
			command_parser.add_argument(
				option,
				dest=destination,
				type=value_type,
				default=argparse.SUPPRESS,
				help=argparse.SUPPRESS,
			)
		return command_parser

	p_put = command("put")
	p_put.add_argument("bucket")
	p_put.add_argument("key")
	p_put.add_argument("file")

	p_get = command("get")
	p_get.add_argument("bucket")
	p_get.add_argument("key")
	p_get.add_argument("out")

	p_ls = command("ls")
	p_ls.add_argument("bucket")
	p_ls.add_argument("--prefix", default="")

	p_move = command("move")
	p_move.add_argument("src")
	p_move.add_argument("dst")
	p_move.add_argument("bucket")
	p_move.add_argument("key")
	p_move.add_argument(
		"--idempotency-key",
		help="Stable key used to resume this durable move after interruption",
	)

	p_move_status = command("move-status", help="Inspect one durable move job")
	p_move_status.add_argument("idempotency_key")

	p_move_list = command("move-list", help="List durable move jobs")
	p_move_list.add_argument(
		"--state",
		action="append",
		choices=[state.value for state in MoveJobState],
		help="Filter by state (repeatable)",
	)
	p_move_list.add_argument("--idempotency-prefix")

	p_move_resume = command("move-resume", help="Resume one durable move job")
	p_move_resume.add_argument("idempotency_key")

	p_lst = command("ls-tier")
	p_lst.add_argument("tier")
	p_lst.add_argument("bucket")
	p_lst.add_argument("--prefix", default="")

	p_scan = command("catalog-scan")
	p_scan.add_argument("tier", help="Tier to scan (e.g., hot)")
	p_scan.add_argument("bucket")
	p_scan.add_argument("--prefix", default="")
	p_scan.add_argument("--sync", action="store_true", help="Run inline instead of enqueueing (development only)")
	p_scan.add_argument("--job-id", help="Optional UUID to use as the logical job ID")
	p_scan.add_argument("--correlation-id", help="Optional request/trace correlation identifier")

	# Profile tiers to derive latency/throughput/capacity metrics
	p_prof = command("tier-profile")
	p_prof.add_argument("--metrics-out", help="Optional path to write JSON metrics for all tiers")

	# Scan device hardware characteristics reported by the OS
	p_dev = command("devices-scan")
	p_dev.add_argument("--hardware-out", help="Optional path to write JSON hardware info for all tiers")

	# Auto-refresh caches: hardware + metrics, once or periodically
	p_auto = command("auto-refresh")
	p_auto.add_argument("--cache-dir", default=".cognistore", help="Directory to store cache files (hardware.json, tier_metrics.json)")
	p_auto.add_argument("--interval", type=int, default=0, help="Seconds between refresh cycles; 0 to run once and exit")

	p_policy = command("policy-run")
	p_policy.add_argument("bucket")
	p_policy.add_argument("--prefix", default="")
	p_policy.add_argument("--threshold", type=int, default=1024*1024, help="Size threshold for policies")
	p_policy.add_argument("--policy", choices=["simple", "llm", "content"], default="simple")
	p_policy.add_argument("--allowed-tiers", default="hot,warm", help="Comma-separated list of allowed tiers")
	p_policy.add_argument("--llm-threshold", type=int, help="Optional override threshold when using --policy llm")
	p_policy.add_argument("--metrics-in", help="Optional JSON metrics from tier-profile to inform policy")
	p_policy.add_argument("--hardware-in", help="Optional JSON hardware info from devices-scan for fallback profiles")
	p_policy.add_argument("--auto-discover", action="store_true", help="If no metrics/hardware provided, auto-scan devices and profile tiers, using a cache with TTL")
	p_policy.add_argument("--cache-dir", default=".cognistore", help="Directory to read/write auto-discover cache files")
	p_policy.add_argument("--cache-ttl", type=int, default=3600, help="Seconds a cache file is considered fresh for auto-discover")
	# Content-aware options
	p_policy.add_argument("--hot-name", action="append", help="Glob pattern(s) for keys that should go to hot")
	p_policy.add_argument("--warm-name", action="append", help="Glob pattern(s) for keys that should go to warm")
	p_policy.add_argument("--hot-mime", action="append", help="MIME prefix(es) that should go to hot, e.g. text/ or image/")
	p_policy.add_argument("--warm-mime", action="append", help="MIME prefix(es) that should go to warm, e.g. application/zip")
	p_policy.add_argument("--cold-name", action="append", help="Glob pattern(s) for keys that should go to cold")
	p_policy.add_argument("--cold-mime", action="append", help="MIME prefix(es) that should go to cold, e.g. application/x-tar")
	p_policy.add_argument("--sync", action="store_true", help="Run writable work inline instead of enqueueing (development only)")
	p_policy.add_argument("--job-id", help="Optional UUID to use as the logical job ID")
	p_policy.add_argument("--correlation-id", help="Optional request/trace correlation identifier")

	p_worker = command("worker", help="Run the durable background worker")
	p_worker.add_argument("--health-host", default="127.0.0.1")
	p_worker.add_argument("--health-port", type=int, default=8081)
	p_worker.add_argument("--fetch-timeout", type=float, default=1.0)
	p_worker.add_argument("--heartbeat-interval", type=float, default=10.0)
	p_worker.add_argument("--shutdown-grace", type=float, default=30.0)
	p_worker.add_argument("--settlement-timeout", type=float, default=5.0)
	p_worker.add_argument(
		"--max-in-flight",
		type=int,
		default=8,
		help="Maximum deliveries concurrently owned by this worker process",
	)
	p_worker.add_argument(
		"--max-ack-pending",
		type=int,
		default=64,
		help="Shared durable-consumer acknowledgement capacity",
	)
	p_worker.add_argument(
		"--tier-limits",
		help="Per-tier throughput YAML; send SIGHUP to reload it safely",
	)
	p_worker.add_argument("--max-attempts", type=int, default=7)
	p_worker.add_argument("--retry-base-delay", type=float, default=1.0)
	p_worker.add_argument("--retry-max-delay", type=float, default=30.0)
	p_worker.add_argument("--retry-jitter", type=float, default=0.2)
	p_worker.add_argument(
		"--schedule-lock-ttl",
		type=float,
		default=60.0,
		help="Scheduled execution lease renewal and failure-detection window in seconds",
	)
	p_worker.add_argument("--once", action="store_true", help="Stop after settling one delivery (primarily for tests)")

	p_scheduler = command(
		"scheduler", help="Enqueue configured periodic control-plane jobs"
	)
	p_scheduler.add_argument(
		"--schedule-config", required=True, help="Path to periodic jobs YAML"
	)
	p_scheduler.add_argument(
		"--poll-interval",
		type=float,
		default=1.0,
		help="Seconds between durable due-job checks",
	)
	p_scheduler.add_argument(
		"--once", action="store_true", help="Publish one due cycle and exit"
	)

	p_redrive = command(
		"dead-letter-redrive",
		aliases=["job-redrive", "dlq-redrive"],
		help="Republish one immutable dead-letter entry",
	)
	p_redrive.add_argument("dead_letter_id")

	parser.set_defaults(**resolution.values)
	args = parser.parse_args(argv)
	args.cmd = _canonical_command(args.cmd)
	type(parser).command_hint = args.cmd
	args.profile = resolution.profile
	args.config = None if resolution.path is None else str(resolution.path)
	args._config_resolution = resolution
	reporter = VerboseReporter(bool(args.verbose))
	effective_keys = (
		"base",
		"drivers",
		"catalog_db",
		"nats_url",
		"job_stream",
		"job_subject",
		"job_consumer",
		"ack_wait",
		"stream_max_messages",
		"stream_max_bytes",
		"dead_letter_stream",
		"dead_letter_subject",
		"dead_letter_max_age",
		"json",
		"dry_run",
		"verbose",
	)
	effective_sources = {
		key: resolution.sources.get(key, "built-in") for key in effective_keys
	}
	effective_sources.update(
		{key: "command-line" for key in _command_line_value_sources(argv)}
	)
	effective_values = {
		key: getattr(args, key, None) for key in effective_keys
	}
	if effective_values["nats_url"] is None:
		effective_values["nats_url"] = ["nats://127.0.0.1:4222"]
	if effective_values["dead_letter_stream"] is None:
		effective_values["dead_letter_stream"] = f"{args.job_stream}_DLQ"
		effective_sources["dead_letter_stream"] = "derived:job_stream"
	if effective_values["dead_letter_subject"] is None:
		effective_values["dead_letter_subject"] = f"{args.job_subject}.dead"
		effective_sources["dead_letter_subject"] = "derived:job_subject"
	reporter(
		"resolved CLI configuration",
		command=args.cmd,
		config=args.config,
		profile=args.profile,
		values=effective_values,
		sources=effective_sources,
	)
	dry_run = bool(getattr(args, "dry_run", False))
	queue_command = (
		args.cmd
		in {
			"worker",
			"scheduler",
			"dead-letter-redrive",
			"job-redrive",
			"dlq-redrive",
		}
		or (
			args.cmd in {"catalog-scan", "policy-run"}
			and not bool(getattr(args, "dry_run", False))
			and not getattr(args, "sync", False)
		)
	)
	if queue_command:
		try:
			_queue_config(args, client_name="cognistore-config-validation")
		except ValueError as exc:
			parser.error(str(exc))
	if args.cmd == "dead-letter-redrive":
		try:
			dead_letter_id = NatsJetStreamQueue._canonical_dead_letter_id(
				args.dead_letter_id
			)
		except Exception as exc:
			_render_redrive_error(exc, json_output=args.json)
			return 1
		if dry_run:
			_emit_result(
				"dead-letter-redrive",
				"planned",
				json_output=args.json,
				human=f"planned redrive dead_letter_id={dead_letter_id}",
				dry_run=True,
				dead_letter_id=dead_letter_id,
				existence_checked=False,
			)
			return 0
		try:
			receipt = asyncio.run(
				_redrive_dead_letter(
					_queue_config(
						args, client_name="cognistore-redrive", one_shot=True
					),
					args.dead_letter_id,
				)
			)
		except Exception as exc:
			_render_redrive_error(exc, json_output=args.json)
			return 1
		_render_redrive(receipt, json_output=args.json)
		return 0

	if args.cmd in {"move-status", "move-list"}:
		if not args.catalog_db or args.catalog_db == ":memory:":
			parser.error(
				"--catalog-db must name an existing persistent SQLite file for "
				f"{args.cmd}"
			)
		catalog_path = Path(args.catalog_db).expanduser()
		if not catalog_path.is_file():
			parser.error(f"--catalog-db does not exist: {catalog_path}")
		status_catalog = open_sqlite_catalog(catalog_path, read_only=True)
		try:
			if args.cmd == "move-status":
				job = status_catalog.get_move_job(args.idempotency_key)
				if job is None:
					return _emit_failure(
						"move-status",
						f"move job not found: {args.idempotency_key}",
						json_output=args.json,
						error_type="MoveJobNotFound",
						idempotency_key=args.idempotency_key,
					)
				transitions = status_catalog.list_move_job_transitions(
					args.idempotency_key
				)
				_emit_result(
					"move-status",
					"success",
					json_output=args.json,
					human=(
						f"{job.idempotency_key} state={job.state.value} "
						f"{job.src_tier}->{job.dst_tier} {job.bucket}/{job.key}"
					),
					job=_move_job_payload(job),
					transitions=[
						_move_transition_payload(transition)
						for transition in transitions
					],
				)
				return 0

			states = (
				None
				if not args.state
				else {MoveJobState(state) for state in args.state}
			)
			jobs = status_catalog.list_move_jobs(
				states=states,
				idempotency_prefix=args.idempotency_prefix,
			)
			jobs.sort(key=lambda job: (job.created_at, job.idempotency_key))
			_emit_result(
				"move-list",
				"success",
				json_output=args.json,
				human=[
					f"{job.idempotency_key} state={job.state.value} "
					f"{job.src_tier}->{job.dst_tier} {job.bucket}/{job.key}"
					for job in jobs
				],
				count=len(jobs),
				jobs=[_move_job_payload(job) for job in jobs],
			)
			return 0
		finally:
			status_catalog.close()

	background_submission = (
		args.cmd in {"catalog-scan", "policy-run"}
		and not dry_run
		and not getattr(args, "sync", False)
	)
	if args.cmd == "worker" and dry_run:
		parser.error(
			"worker does not support --dry-run because consuming queued jobs has "
			"writable effects; preview catalog-scan and policy-run submissions instead"
		)
	if args.cmd in {"move", "move-resume"}:
		idempotency_key = getattr(args, "idempotency_key", None)
		if idempotency_key is not None and not idempotency_key.strip():
			parser.error("--idempotency-key must not be blank")
		if args.cmd == "move-resume" or idempotency_key is not None:
			if not args.catalog_db or args.catalog_db == ":memory:":
				parser.error(
					"a persistent --catalog-db is required for resumable moves"
				)
			if args.cmd == "move-resume" and not Path(
				args.catalog_db
			).expanduser().is_file():
				parser.error("--catalog-db must already exist for move-resume")
	driver: StorageDriver | None = None
	drivers: dict[str, StorageDriver] | None = None
	if args.drivers:
		drivers = load_drivers(args.drivers)
	else:
		if not args.base:
			parser.error("either --drivers or --base is required")
		driver = PosixDriver(args.base)

	if args.cmd not in {"put", "get", "ls"} and not drivers:
		parser.error("--drivers is required for tier operations")
	if args.cmd == "worker" and not args.catalog_db:
		parser.error("--catalog-db is required for worker")
	if args.cmd == "scheduler" and not dry_run and not args.catalog_db:
		parser.error("--catalog-db is required for scheduler state")
	if (
		args.cmd in {"worker", "scheduler"}
		and not (args.cmd == "scheduler" and dry_run)
		and args.catalog_db == ":memory:"
	):
		parser.error(
			"--catalog-db must be a persistent SQLite file for worker and scheduler"
		)
	if args.cmd == "worker" and args.heartbeat_interval >= args.ack_wait:
		parser.error("--heartbeat-interval must be less than --ack-wait")
	if args.cmd == "worker" and args.max_ack_pending < args.max_in_flight:
		parser.error("--max-ack-pending must be at least --max-in-flight")
	if args.cmd == "worker":
		if not math.isfinite(args.schedule_lock_ttl) or args.schedule_lock_ttl <= 0:
			parser.error("--schedule-lock-ttl must be positive and finite")
		try:
			schedule_lock_duration = timedelta(seconds=args.schedule_lock_ttl)
		except OverflowError:
			parser.error("--schedule-lock-ttl must not exceed 100 years")
		if schedule_lock_duration <= timedelta(0):
			parser.error("--schedule-lock-ttl must be at least one microsecond")
		if args.schedule_lock_ttl < 0.000001:
			parser.error("--schedule-lock-ttl must be at least one microsecond")
		if schedule_lock_duration.total_seconds() != args.schedule_lock_ttl:
			parser.error("--schedule-lock-ttl must use whole-microsecond precision")
		if schedule_lock_duration > timedelta(days=36_525):
			parser.error("--schedule-lock-ttl must not exceed 100 years")
		try:
			WorkerConfig(
				fetch_timeout=args.fetch_timeout,
				heartbeat_interval=args.heartbeat_interval,
				shutdown_grace=args.shutdown_grace,
				settlement_timeout=args.settlement_timeout,
				max_in_flight=args.max_in_flight,
				max_attempts=args.max_attempts,
				retry_base_delay=args.retry_base_delay,
				retry_max_delay=args.retry_max_delay,
				retry_jitter=args.retry_jitter,
			)
		except ValueError as exc:
			parser.error(str(exc))
		assert drivers is not None
		try:
			args._throughput_config = _throughput_config(args, drivers)
		except (OSError, ValueError) as exc:
			parser.error(f"invalid --tier-limits configuration: {exc}")
	if args.cmd == "scheduler":
		if not math.isfinite(args.poll_interval) or args.poll_interval <= 0:
			parser.error("--poll-interval must be positive and finite")
		assert drivers is not None
		try:
			args._schedules = load_schedule_config(
				args.schedule_config, known_tiers=drivers
			)
		except (OSError, ValueError) as exc:
			parser.error(f"invalid --schedule-config: {exc}")
	if (
		background_submission
		and "catalog_db" in _command_line_value_sources(argv)
	):
		parser.error(
			"--catalog-db configures inline work only; background jobs use the "
			"worker's --catalog-db"
		)
	policy_allowed = None
	existing_move_job: MoveJob | None = None
	if args.cmd == "policy-run":
		assert drivers is not None
		policy_allowed = tuple(
			dict.fromkeys(t.strip() for t in args.allowed_tiers.split(",") if t.strip())
		)
		if not policy_allowed:
			parser.error("--allowed-tiers must contain at least one tier")
		unknown_allowed = sorted(set(policy_allowed).difference(drivers))
		if unknown_allowed:
			parser.error(f"unknown allowed tier(s): {', '.join(unknown_allowed)}")
	elif args.cmd == "move":
		assert drivers is not None
		if args.idempotency_key is not None:
			move_catalog_path = Path(args.catalog_db).expanduser()
			if move_catalog_path.is_file():
				lookup_catalog = open_sqlite_catalog(
					move_catalog_path, read_only=True
				)
				try:
					existing_move_job = lookup_catalog.get_move_job(
						args.idempotency_key
					)
				finally:
					lookup_catalog.close()
			if existing_move_job is not None:
				expected_identity = (
					existing_move_job.src_tier,
					existing_move_job.dst_tier,
					existing_move_job.bucket,
					existing_move_job.key,
				)
				requested_identity = (args.src, args.dst, args.bucket, args.key)
				if requested_identity != expected_identity:
					parser.error(
						"idempotency key is already assigned to a different move"
					)
				try:
					Mover(drivers, Catalog()).validate_driver_pair(args.src, args.dst)
				except ValueError as exc:
					parser.error(str(exc))
		# A new move keeps the safety guarantee that the complete storage plan is
		# validated before a writable catalog is opened.  Existing keyed jobs skip
		# destination-collision preflight so their durable phase can resume.
		if existing_move_job is None:
			try:
				Mover(drivers, Catalog()).plan(
					args.src, args.dst, args.bucket, args.key
				)
			except (ValueError, FileExistsError, FileNotFoundError) as exc:
				parser.error(str(exc))
	elif args.cmd == "move-resume":
		assert drivers is not None
		lookup_catalog = open_sqlite_catalog(args.catalog_db, read_only=True)
		try:
			existing_move_job = lookup_catalog.get_move_job(args.idempotency_key)
		finally:
			lookup_catalog.close()
		if existing_move_job is None:
			return _emit_failure(
				"move-resume",
				f"move job not found: {args.idempotency_key}",
				json_output=args.json,
				error_type="MoveJobNotFound",
				idempotency_key=args.idempotency_key,
			)
		try:
			Mover(drivers, Catalog()).validate_driver_pair(
				existing_move_job.src_tier,
				existing_move_job.dst_tier,
			)
		except ValueError as exc:
			parser.error(str(exc))

	catalog: Catalog | None
	catalog_commands = {
		"worker",
		"move",
		"move-resume",
		"catalog-scan",
		"policy-run",
	}
	if (
		background_submission
		or args.cmd == "scheduler"
		or args.cmd not in catalog_commands
	):
		catalog = None
	elif args.catalog_db and args.cmd == "policy-run" and dry_run:
		if not Path(args.catalog_db).expanduser().exists():
			parser.error("--catalog-db must already exist for policy-run --dry-run")
		catalog = open_sqlite_catalog(args.catalog_db, read_only=True)
	elif args.catalog_db and args.cmd in {"move", "move-resume"} and dry_run:
		move_catalog_path = Path(args.catalog_db).expanduser()
		catalog = (
			open_sqlite_catalog(move_catalog_path, read_only=True)
			if move_catalog_path.is_file()
			else Catalog()
		)
	elif args.cmd == "catalog-scan" and dry_run:
		catalog = None
	elif args.catalog_db:
		catalog = open_sqlite_catalog(args.catalog_db)
	else:
		catalog = Catalog()

	if args.cmd == "scheduler":
		if dry_run:
			schedules = [
				{
					"schedule_id": schedule.schedule_id,
					"job_type": schedule.job_type,
					"interval_seconds": schedule.interval_seconds,
					"enabled": schedule.enabled,
					"payload": dict(schedule.payload),
				}
				for schedule in args._schedules
			]
			_emit_result(
				"scheduler",
				"planned",
				json_output=args.json,
				human=[
					f"planned schedule {item['schedule_id']} type={item['job_type']}"
					for item in schedules
				],
				dry_run=True,
				count=len(schedules),
				schedules=schedules,
				due_state_checked=False,
			)
			return 0
		return asyncio.run(_serve_scheduler(args, args._schedules))

	if args.cmd == "worker":
		assert drivers is not None
		assert isinstance(catalog, SQLiteCatalog)
		try:
			return asyncio.run(_serve_worker(args, drivers, catalog))
		finally:
			catalog.close()

	if args.cmd == "put":
		source_path = Path(args.file)
		if not source_path.is_file():
			raise FileNotFoundError(f"input file does not exist: {source_path}")
		if driver is None:
			assert drivers is not None
			active_driver = drivers["hot"]
		else:
			active_driver = driver
		if dry_run:
			try:
				active_driver.stat_object(args.bucket, args.key)
			except FileNotFoundError:
				would_overwrite = False
			else:
				would_overwrite = True
			_emit_result(
				"put",
				"planned",
				json_output=args.json,
				human=(
					f"planned put {args.bucket}/{args.key} from {source_path}"
					f" overwrite={would_overwrite}"
				),
				dry_run=True,
				bucket=args.bucket,
				key=args.key,
				file=str(source_path),
				size=source_path.stat().st_size,
				would_overwrite=would_overwrite,
			)
			return 0
		data = source_path.read_bytes()
		active_driver.put_object(args.bucket, args.key, data)
		_emit_result(
			"put",
			"completed",
			json_output=args.json,
			bucket=args.bucket,
			key=args.key,
			file=str(source_path),
			size=len(data),
			dry_run=False,
		)
		return 0
	if args.cmd == "get":
		if driver is None:
			assert drivers is not None
			active_driver = drivers["hot"]
		else:
			active_driver = driver
		out_path = Path(args.out)
		if dry_run:
			metadata = active_driver.stat_object(args.bucket, args.key)
			_emit_result(
				"get",
				"planned",
				json_output=args.json,
				human=f"planned get {args.bucket}/{args.key} -> {out_path}",
				dry_run=True,
				bucket=args.bucket,
				key=args.key,
				out=str(out_path),
				size=int(metadata.get("size", 0)),
				would_overwrite=out_path.exists(),
			)
			return 0
		data = active_driver.get_object(args.bucket, args.key)
		out_path.parent.mkdir(parents=True, exist_ok=True)
		out_path.write_bytes(data)
		_emit_result(
			"get",
			"completed",
			json_output=args.json,
			bucket=args.bucket,
			key=args.key,
			out=str(out_path),
			size=len(data),
			dry_run=False,
		)
		return 0
	if args.cmd == "ls":
		if driver is None:
			assert drivers is not None
			active_driver = drivers["hot"]
		else:
			active_driver = driver
		keys = list(active_driver.list_objects(args.bucket, prefix=args.prefix))
		_emit_result(
			"ls",
			"success",
			json_output=args.json,
			human=keys,
			dry_run=dry_run,
			bucket=args.bucket,
			prefix=args.prefix,
			count=len(keys),
			keys=keys,
		)
		return 0

	assert drivers is not None

	# move between tiers
	if args.cmd == "move":
		assert catalog is not None
		if (
			existing_move_job is not None
			and existing_move_job.state == MoveJobState.FAILED
		):
			if isinstance(catalog, SQLiteCatalog):
				catalog.close()
			return _emit_failure(
				"move",
				existing_move_job.terminal_reason or "move job is terminally failed",
				json_output=args.json,
				error_type="MoveJobFailedError",
				idempotency_key=existing_move_job.idempotency_key,
				job=_move_job_payload(existing_move_job),
			)
		already_completed = (
			existing_move_job is not None
			and existing_move_job.state == MoveJobState.COMPLETED
		)
		would_resume = existing_move_job is not None and not already_completed
		mv = Mover(drivers, catalog)
		verification = None
		move_key = args.idempotency_key
		if dry_run:
			status: Literal["planned", "completed"] = "planned"
		else:
			move_key = move_key or str(uuid4())
			verification = mv.move(
				args.src,
				args.dst,
				args.bucket,
				args.key,
				idempotency_key=move_key,
			)
			status = "completed"
		action = ActionResult(
			bucket=args.bucket,
			key=args.key,
			from_tier=args.src,
			to_tier=args.dst,
			reason=(
				"manual move already completed"
				if already_completed
				else "manual move resume"
				if would_resume
				else "manual move"
			),
			status=status,
		)
		_render_actions(
			[action],
			command="move",
			dry_run=dry_run,
			json_output=args.json,
			extra={
				"idempotency_key": move_key,
				"would_resume": would_resume,
				"outcome": (
					"already_completed"
					if already_completed
					else "would_resume"
					if dry_run and would_resume
					else "would_move"
					if dry_run
					else "completed"
				),
				"job": (
					None
					if existing_move_job is None
					else _move_job_payload(existing_move_job)
				),
				"verification": None if verification is None else asdict(verification),
			},
		)
		if isinstance(catalog, SQLiteCatalog):
			catalog.close()
		return 0

	if args.cmd == "move-resume":
		assert catalog is not None
		assert existing_move_job is not None
		if existing_move_job.state == MoveJobState.FAILED:
			if isinstance(catalog, SQLiteCatalog):
				catalog.close()
			return _emit_failure(
				"move-resume",
				existing_move_job.terminal_reason or "move job is terminally failed",
				json_output=args.json,
				error_type="MoveJobFailedError",
				job=_move_job_payload(existing_move_job),
			)
		if dry_run:
			_emit_result(
				"move-resume",
				"planned",
				json_output=args.json,
				human=(
					f"planned resume {existing_move_job.idempotency_key} "
					f"from state={existing_move_job.state.value}"
				),
				dry_run=True,
				outcome=(
					"already_completed"
					if existing_move_job.state == MoveJobState.COMPLETED
					else "would_resume"
				),
				job=_move_job_payload(existing_move_job),
				verification=None,
			)
			if isinstance(catalog, SQLiteCatalog):
				catalog.close()
			return 0
		if existing_move_job.state == MoveJobState.COMPLETED:
			_emit_result(
				"move-resume",
				"completed",
				json_output=args.json,
				human=f"move {existing_move_job.idempotency_key} already completed",
				dry_run=False,
				outcome="already_completed",
				job=_move_job_payload(existing_move_job),
				verification=None,
			)
			if isinstance(catalog, SQLiteCatalog):
				catalog.close()
			return 0
		mv = Mover(drivers, catalog)
		verification = mv.move(
			existing_move_job.src_tier,
			existing_move_job.dst_tier,
			existing_move_job.bucket,
			existing_move_job.key,
			idempotency_key=existing_move_job.idempotency_key,
		)
		updated_job = mv.get_job(existing_move_job.idempotency_key)
		_emit_result(
			"move-resume",
			"completed",
			json_output=args.json,
			human=f"resumed move {existing_move_job.idempotency_key}",
			dry_run=False,
			outcome="completed",
			job=(
				_move_job_payload(updated_job)
				if updated_job is not None
				else _move_job_payload(existing_move_job)
			),
			verification=asdict(verification),
		)
		if isinstance(catalog, SQLiteCatalog):
			catalog.close()
		return 0

	if args.cmd == "ls-tier":
		tier = args.tier
		if tier not in drivers:
			parser.error(f"unknown tier: {tier}")
		keys = list(drivers[tier].list_objects(args.bucket, prefix=args.prefix))
		_emit_result(
			"ls-tier",
			"success",
			json_output=args.json,
			human=keys,
			dry_run=dry_run,
			tier=tier,
			bucket=args.bucket,
			prefix=args.prefix,
			count=len(keys),
			keys=keys,
		)
		return 0

	if args.cmd == "catalog-scan":
		if args.tier not in drivers:
			parser.error(f"unknown tier: {args.tier}")
		if background_submission:
			enqueue_job = JobEnvelope.create(
				CATALOG_SCAN_JOB,
				{"tier": args.tier, "bucket": args.bucket, "prefix": args.prefix},
				job_id=args.job_id,
				correlation_id=args.correlation_id,
			)
			return _submit_job(args, enqueue_job)

		if not dry_run:
			assert catalog is not None
		scan_results = scan_catalog(
			tier=args.tier,
			bucket=args.bucket,
			prefix=args.prefix,
			driver=drivers[args.tier],
			catalog=None if dry_run else catalog,
			dry_run=dry_run,
		)
		verb = "planned index" if dry_run else "indexed"
		items = [asdict(result) for result in scan_results]
		_emit_result(
			"catalog-scan",
			"planned" if dry_run else "completed",
			json_output=args.json,
			human=[
				f"{verb} {result.tier}:{result.bucket}/{result.key} size={result.size}"
				for result in scan_results
			],
			dry_run=dry_run,
			count=len(items),
			objects=items,
		)
		if isinstance(catalog, SQLiteCatalog):
			catalog.close()
		return 0

	if args.cmd == "tier-profile":
		if not drivers:
			parser.error("--drivers is required for tier-profile")
		profile_results: dict[str, Any] = {}
		planned_tiers: list[dict[str, str]] = []
		human: list[str] = []
		for tier, drv in drivers.items():
			base_path = None
			if isinstance(drv, PosixDriver):
				base_path = drv.base
			else:
				base_path = getattr(drv, "base", None) or getattr(drv, "base_path", None)
			if not base_path:
				print(f"skip {tier}: unsupported driver for profiling", file=sys.stderr)
				continue
			if dry_run:
				planned_tiers.append({"tier": tier, "path": str(base_path)})
				human.append(f"planned profile {tier}: path={base_path}")
				continue
			m = profile_path(str(base_path))
			profile_results[tier] = m
			human.append(
				f"{tier}: read={m.seq_read_MBps:.1f} MB/s "
				f"write={m.seq_write_MBps:.1f} MB/s "
				f"randIOPS={m.random_read_IOPS:.0f} "
				f"fbyte={m.first_byte_latency_ms:.2f} ms "
				f"free={m.free_bytes/1e9:.1f}G/{m.total_bytes/1e9:.1f}G"
			)
		if args.metrics_out and not dry_run:
			save_metrics_json(profile_results, args.metrics_out)
			human.append(f"wrote metrics -> {args.metrics_out}")
		_emit_result(
			"tier-profile",
			"planned" if dry_run else "completed",
			json_output=args.json,
			human=human,
			dry_run=dry_run,
			metrics_out=args.metrics_out,
			would_write=args.metrics_out if dry_run else None,
			planned_tiers=planned_tiers,
			metrics={
				tier: metrics.to_dict() for tier, metrics in profile_results.items()
			},
		)
		return 0

	if args.cmd == "devices-scan":
		if not drivers:
			parser.error("--drivers is required for devices-scan")
		info: dict[str, Any] = {}
		human = []
		for tier, drv in drivers.items():
			base_path = None
			if isinstance(drv, PosixDriver):
				base_path = drv.base
			else:
				base_path = getattr(drv, "base", None) or getattr(drv, "base_path", None)
			if not base_path:
				print(f"skip {tier}: unsupported driver for devices-scan", file=sys.stderr)
				continue
			di = discover_device_for_tier(tier, str(base_path))
			info[tier] = di
			mt = di.media_type
			human.append(
				f"{tier}: dev={di.device or '?'} base={di.base_device or '?'} "
				f"type={mt} model={di.model or '?'} transport={di.transport or '?'}"
			)
		if args.hardware_out and not dry_run:
			save_hardware_json(info, args.hardware_out)
			human.append(f"wrote hardware -> {args.hardware_out}")
		elif args.hardware_out:
			human.append(f"planned write hardware -> {args.hardware_out}")
		_emit_result(
			"devices-scan",
			"planned" if dry_run else "completed",
			json_output=args.json,
			human=human,
			dry_run=dry_run,
			hardware_out=args.hardware_out,
			would_write=args.hardware_out if dry_run else None,
			devices={tier: device.to_dict() for tier, device in info.items()},
		)
		return 0

	if args.cmd == "auto-refresh":
		if not drivers:
			parser.error("--drivers is required for auto-refresh")
		cache_dir = Path(args.cache_dir)
		hw_path = cache_dir / "hardware.json"
		m_path = cache_dir / "tier_metrics.json"
		if dry_run:
			_emit_result(
				"auto-refresh",
				"planned",
				json_output=args.json,
				human=[
					f"planned refresh hardware -> {hw_path}",
					f"planned refresh metrics -> {m_path}",
				],
				dry_run=True,
				interval=args.interval,
				tiers=sorted(drivers),
				outputs={"hardware": str(hw_path), "metrics": str(m_path)},
			)
			return 0
		cache_dir.mkdir(parents=True, exist_ok=True)

		def do_refresh() -> tuple[dict[str, Any], dict[str, Any]]:
			# Hardware scan
			info = {}
			for tier, drv in drivers.items():
				base_path = None
				if isinstance(drv, PosixDriver):
					base_path = drv.base
				else:
					base_path = getattr(drv, "base", None) or getattr(drv, "base_path", None)
				if not base_path:
					print(f"skip {tier}: unsupported driver for auto-refresh", file=sys.stderr)
					continue
				di = discover_device_for_tier(tier, str(base_path))
				info[tier] = di
			save_hardware_json(info, str(hw_path))
			# Metrics profile
			refresh_metrics: dict[str, Any] = {}
			for tier, drv in drivers.items():
				base_path = None
				if isinstance(drv, PosixDriver):
					base_path = drv.base
				else:
					base_path = getattr(drv, "base", None) or getattr(drv, "base_path", None)
				if not base_path:
					continue
				m = profile_path(str(base_path))
				refresh_metrics[tier] = m
			save_metrics_json(refresh_metrics, str(m_path))
			return info, refresh_metrics

		def render_refresh(
			info: dict[str, Any], refresh_metrics: dict[str, Any]
		) -> None:
			_emit_result(
				"auto-refresh",
				"completed",
				json_output=args.json,
				human=[
					f"refreshed hardware -> {hw_path}",
					f"refreshed metrics -> {m_path}",
				],
				dry_run=False,
				interval=args.interval,
				outputs={"hardware": str(hw_path), "metrics": str(m_path)},
				devices={tier: device.to_dict() for tier, device in info.items()},
				metrics={
					tier: metrics.to_dict()
					for tier, metrics in refresh_metrics.items()
				},
			)
			sys.stdout.flush()

		if args.interval and args.interval > 0:
			try:
				while True:
					render_refresh(*do_refresh())
					time.sleep(args.interval)
			except KeyboardInterrupt:
				return 0
		else:
			render_refresh(*do_refresh())
			return 0

	if args.cmd == "policy-run":
		assert policy_allowed is not None
		allowed = policy_allowed

		def _policy_info(message: str) -> None:
			print(redact_text(message), file=sys.stderr if args.json else sys.stdout)

		# Optionally derive a data-driven size threshold from measured tier metrics
		derived_threshold = None
		# Auto-discover cache files if requested and no explicit inputs provided
		if args.auto_discover and not args.metrics_in and not args.hardware_in:
			cache_dir = Path(args.cache_dir)
			hw_path = cache_dir / "hardware.json"
			m_path = cache_dir / "tier_metrics.json"
			# Refresh caches if stale/missing
			now = time.time()
			# helper to compute age
			def _is_stale(p: Path) -> bool:
				try:
					st = p.stat()
					return (now - st.st_mtime) > args.cache_ttl
				except FileNotFoundError:
					return True
			if args.dry_run:
				# A preview may consume a fresh cache, but it must never refresh
				# one: profiling performs writes inside configured tier roots.
				if not _is_stale(m_path):
					args.metrics_in = str(m_path)
				elif not _is_stale(hw_path):
					args.hardware_in = str(hw_path)
				else:
					print(
						"warning: dry-run skipped auto-discovery refresh; using the configured threshold",
						file=sys.stderr,
					)
			else:
				cache_dir.mkdir(parents=True, exist_ok=True)
				# Hardware
				if _is_stale(hw_path):
					info = {}
					for tier, drv in drivers.items():
						base_path = getattr(drv, "base", None) or getattr(drv, "base_path", None)
						if not base_path:
							continue
						di = discover_device_for_tier(tier, str(base_path))
						info[tier] = di
					save_hardware_json(info, str(hw_path))
					_policy_info(f"[auto] wrote {hw_path}")
				# Metrics
				if _is_stale(m_path):
					results = {}
					for tier, drv in drivers.items():
						base_path = getattr(drv, "base", None) or getattr(drv, "base_path", None)
						if not base_path:
							continue
						m = profile_path(str(base_path))
						results[tier] = m
					save_metrics_json(results, str(m_path))
					_policy_info(f"[auto] wrote {m_path}")
				# Point inputs to caches for downstream logic
				if m_path.exists():
					args.metrics_in = str(m_path)
				elif hw_path.exists():
					args.hardware_in = str(hw_path)
		if args.metrics_in:
			try:
				metrics = load_metrics_json(args.metrics_in)
				metrics_hot = metrics.get("hot")
				metrics_warm = metrics.get("warm")
				if metrics_hot and metrics_warm:
					# Convert to seconds and bytes/sec
					Lh = max(metrics_hot.first_byte_latency_ms, 0.01) / 1000.0
					Lw = max(metrics_warm.first_byte_latency_ms, 0.01) / 1000.0
					Bh = max(metrics_hot.seq_read_MBps, 0.1) * 1024 * 1024
					Bw = max(metrics_warm.seq_read_MBps, 0.1) * 1024 * 1024
					denom = (1.0 / Bw) - (1.0 / Bh)
					if denom > 0:
						bytes_switch = int((Lw - Lh) / denom)
						# Clamp to a reasonable range [64KiB, 1GiB]
						bytes_switch = max(64 * 1024, min(bytes_switch, 1024 * 1024 * 1024))
						derived_threshold = bytes_switch
						_policy_info(f"[policy] derived hot/warm threshold from metrics: {bytes_switch/1024/1024:.2f} MiB")
			except Exception as e:
				print(
					redact_text(
						f"warning: failed to load/derive metrics from "
						f"{args.metrics_in}: {e}"
					),
					file=sys.stderr,
				)
		elif args.hardware_in:
			# Fallback: use OS hardware classification + default profiles
			try:
				hw = load_hardware_json(args.hardware_in)
				hardware_hot = hw.get("hot")
				hardware_warm = hw.get("warm")
				if hardware_hot and hardware_warm:
					ph = DEFAULT_PROFILES.get(hardware_hot.media_type, DEFAULT_PROFILES["unknown"])
					pw = DEFAULT_PROFILES.get(hardware_warm.media_type, DEFAULT_PROFILES["unknown"])
					Lh = ph.first_byte_latency_ms / 1000.0
					Lw = pw.first_byte_latency_ms / 1000.0
					Bh = ph.seq_read_MBps * 1024 * 1024
					Bw = pw.seq_read_MBps * 1024 * 1024
					denom = (1.0 / Bw) - (1.0 / Bh)
					if denom > 0:
						bytes_switch = int((Lw - Lh) / denom)
						bytes_switch = max(64 * 1024, min(bytes_switch, 1024 * 1024 * 1024))
						derived_threshold = bytes_switch
						_policy_info(f"[policy] derived hot/warm threshold from hardware: {bytes_switch/1024/1024:.2f} MiB")
			except Exception as e:
				print(
					redact_text(
						f"warning: failed to load/derive hardware from "
						f"{args.hardware_in}: {e}"
					),
					file=sys.stderr,
				)
		effective_threshold = (
			derived_threshold if derived_threshold is not None else args.threshold
		)
		policy_threshold = (
			args.threshold if args.policy == "llm" else effective_threshold
		)
		if background_submission:
			enqueue_job = JobEnvelope.create(
				POLICY_RUN_JOB,
				policy_job_payload(
					bucket=args.bucket,
					prefix=args.prefix,
					policy=args.policy,
					threshold=policy_threshold,
					llm_threshold=args.llm_threshold,
					allowed_tiers=allowed,
					hot_name_patterns=args.hot_name or (),
					warm_name_patterns=args.warm_name or (),
					cold_name_patterns=args.cold_name or (),
					hot_mime_prefixes=args.hot_mime or (),
					warm_mime_prefixes=args.warm_mime or (),
					cold_mime_prefixes=args.cold_mime or (),
				),
				job_id=args.job_id,
				correlation_id=args.correlation_id,
			)
			return _submit_job(args, enqueue_job)

		policy = build_policy(
			args.policy,
			threshold=policy_threshold,
			llm_threshold=args.llm_threshold,
			allowed_tiers=allowed,
			hot_name_patterns=args.hot_name or (),
			warm_name_patterns=args.warm_name or (),
			cold_name_patterns=args.cold_name or (),
			hot_mime_prefixes=args.hot_mime or (),
			warm_mime_prefixes=args.warm_mime or (),
			cold_mime_prefixes=args.cold_mime or (),
		)
		assert catalog is not None
		mv = Mover(drivers, catalog)
		runner = PolicyRunner(catalog, drivers, mv, policy, allowed_tiers=allowed)
		actions = runner.run_once(args.bucket, prefix=args.prefix, dry_run=args.dry_run)
		_render_actions(
			actions,
			command="policy-run",
			dry_run=dry_run,
			json_output=args.json,
		)
		if isinstance(catalog, SQLiteCatalog):
			catalog.close()
		return 0
	return 1


def _early_json_requested(argv: Sequence[str]) -> bool:
	configured = os.environ.get("COGNISTORE_JSON", "").strip().lower()
	requested = configured in {"1", "true", "yes", "on"}
	for argument in argv:
		if argument == "--":
			break
		if argument == "--json":
			requested = True
		elif argument == "--no-json":
			requested = False
	return requested


def main(argv: Sequence[str] | None = None) -> int:
	arguments = list(sys.argv[1:] if argv is None else argv)
	command = _command_hint(arguments)
	json_output = _early_json_requested(arguments)
	try:
		resolution = resolve_cli_config(arguments)
	except CliConfigError as exc:
		return _emit_failure(
			command,
			exc,
			json_output=json_output,
			exit_code=2,
			error_type="ConfigurationError",
		)

	json_output = json_requested(arguments, resolution)
	_CliArgumentParser.json_output = json_output
	_CliArgumentParser.command_hint = command
	verbose = bool(resolution.values.get("verbose", False)) or any(
		argument == "--verbose"
		or (argument.startswith("-v") and not argument.startswith("--"))
		for argument in arguments
	)
	reporter = VerboseReporter(verbose)
	try:
		with _cli_logging(verbose), ExitStack() as catalog_stack:
			return _run_cli(arguments, resolution, catalog_stack)
	except KeyboardInterrupt:
		reporter("command interrupted", command=command)
		return _emit_failure(
			command,
			"interrupted",
			json_output=json_output,
			exit_code=130,
			error_type="Interrupted",
		)
	except Exception as exc:
		reporter(
			"command failed",
			command=command,
			error_type=type(exc).__name__,
			error=str(exc),
		)
		return _emit_failure(
			command,
			exc,
			json_output=json_output,
			retryable=bool(getattr(exc, "retryable", False)),
		)


if __name__ == "__main__":
	raise SystemExit(main())
