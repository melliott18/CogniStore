from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Literal

from cognistore.core.catalog import Catalog
from cognistore.core.mover import Mover
from cognistore.core.policy_factory import build_policy
from cognistore.core.policy_runner import ActionResult, PolicyRunner
from cognistore.core.scanner import scan_catalog
from cognistore.core.sqlite_catalog import SQLiteCatalog
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
	NatsJetStreamConfig,
	NatsJetStreamQueue,
)
from cognistore.jobs.runtime import AsyncWorker, WorkerConfig, WorkerState
from cognistore.utils.device_info import (
	discover_device_for_tier,
	load_hardware_json,
	save_hardware_json,
)
from cognistore.utils.drive_profiles import DEFAULT_PROFILES
from cognistore.utils.tier_profiler import load_metrics_json, profile_path, save_metrics_json

LOGGER = logging.getLogger(__name__)


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
		except Exception:
			LOGGER.warning("failed to close NATS publisher", exc_info=True)


async def _redrive_dead_letter(config: NatsJetStreamConfig, dead_letter_id: str):
	dead_letter_id = NatsJetStreamQueue._canonical_dead_letter_id(dead_letter_id)
	queue = NatsJetStreamQueue(config, consume=False)
	try:
		await queue.connect()
		return await queue.redrive_dead_letter(dead_letter_id)
	finally:
		try:
			await queue.close(graceful=False)
		except Exception:
			LOGGER.warning("failed to close NATS redrive connection", exc_info=True)


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
	if json_output:
		print(json.dumps(payload, sort_keys=True))
	else:
		print(
			f"queued {job.job_type} job_id={job.job_id} "
			f"correlation_id={job.correlation_id}"
		)


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
	if json_output:
		print(json.dumps(payload, sort_keys=True))
	else:
		verb = "already redriven" if receipt.duplicate else "redriven"
		print(
			f"{verb} job_id={receipt.job_id} "
			f"dead_letter_id={receipt.dead_letter_id} "
			f"redrive_count={receipt.redrive_count}"
		)


def _render_redrive_error(exc: Exception, *, json_output: bool) -> None:
	payload = {
		"status": "error",
		"operation": "dead-letter-redrive",
		"error_type": type(exc).__name__,
		"error": str(exc),
	}
	if json_output:
		print(json.dumps(payload, sort_keys=True))
	else:
		print(f"redrive failed: {exc}", file=sys.stderr)


async def _serve_worker(
	args: argparse.Namespace, drivers, catalog: SQLiteCatalog
) -> int:
	queue = NatsJetStreamQueue(_queue_config(args, client_name="cognistore-worker"))
	worker = AsyncWorker(
		queue,
		build_handlers(drivers, catalog),
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
		),
	)
	health = HealthServer(worker, host=args.health_host, port=args.health_port)
	loop = asyncio.get_running_loop()
	installed_signals: list[signal.Signals] = []
	try:
		await worker.start()
		await health.start()
		for signum in (signal.SIGINT, signal.SIGTERM):
			try:
				loop.add_signal_handler(signum, worker.request_shutdown)
				installed_signals.append(signum)
			except (NotImplementedError, RuntimeError):
				pass
		print(
			f"worker ready health=http://{args.health_host}:{health.bound_port} "
			f"stream={args.job_stream} consumer={args.job_consumer}",
			flush=True,
		)
		await worker.wait_for_shutdown_request()
		report = await worker.shutdown()
		return 0 if report.graceful and worker.state == WorkerState.STOPPED else 1
	finally:
		for signum in installed_signals:
			loop.remove_signal_handler(signum)
		if worker.state not in (WorkerState.STOPPED, WorkerState.FAILED):
			await worker.shutdown()
		await health.close()


def _render_actions(actions: list[ActionResult], *, dry_run: bool, json_output: bool) -> None:
	if json_output:
		print(
			json.dumps(
				{
					"dry_run": dry_run,
					"count": len(actions),
					"actions": [
						{
							"status": action.status,
							"bucket": action.bucket,
							"key": action.key,
							"from_tier": action.from_tier,
							"to_tier": action.to_tier,
							"reason": action.reason,
						}
						for action in actions
					],
				},
				sort_keys=True,
			)
		)
		return

	for action in actions:
		verb = "planned" if action.status == "planned" else "moved"
		print(
			f"{verb} {action.bucket}/{action.key} "
			f"{action.from_tier}->{action.to_tier} : {action.reason}"
		)
	summary = "planned_actions" if dry_run else "completed_actions"
	print(f"{summary}={len(actions)}")


def main(argv=None):
	parser = argparse.ArgumentParser(prog="cognistore", description="CogniStore CLI")
	parser.add_argument("--base", help="Base path for POSIX storage (used when --drivers is not provided)")
	parser.add_argument("--drivers", help="Path to drivers.yaml to enable multi-tier operations")
	parser.add_argument("--catalog-db", help="Path to SQLite catalog DB; if omitted uses in-memory catalog")
	parser.add_argument(
		"--nats-url",
		action="append",
		help="NATS server URL (repeat for a cluster; defaults to COGNISTORE_NATS_URL)",
	)
	parser.add_argument("--job-stream", default="COGNISTORE_JOBS")
	parser.add_argument("--job-subject", default="cognistore.jobs")
	parser.add_argument("--job-consumer", default="cognistore-workers")
	parser.add_argument("--ack-wait", type=float, default=30.0, help="Seconds before an unacknowledged job is redelivered")
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

	p_put = sub.add_parser("put")
	p_put.add_argument("bucket")
	p_put.add_argument("key")
	p_put.add_argument("file")

	p_get = sub.add_parser("get")
	p_get.add_argument("bucket")
	p_get.add_argument("key")
	p_get.add_argument("out")

	p_ls = sub.add_parser("ls")
	p_ls.add_argument("bucket")
	p_ls.add_argument("--prefix", default="")

	p_move = sub.add_parser("move")
	p_move.add_argument("src")
	p_move.add_argument("dst")
	p_move.add_argument("bucket")
	p_move.add_argument("key")
	p_move.add_argument("--dry-run", action="store_true", help="Validate and plan the move without writing")
	p_move.add_argument("--json", action="store_true", help="Emit machine-readable action output")

	p_lst = sub.add_parser("ls-tier")
	p_lst.add_argument("tier")
	p_lst.add_argument("bucket")
	p_lst.add_argument("--prefix", default="")

	p_scan = sub.add_parser("catalog-scan")
	p_scan.add_argument("tier", help="Tier to scan (e.g., hot)")
	p_scan.add_argument("bucket")
	p_scan.add_argument("--prefix", default="")
	p_scan.add_argument("--sync", action="store_true", help="Run inline instead of enqueueing (development only)")
	p_scan.add_argument("--job-id", help="Optional UUID to use as the logical job ID")
	p_scan.add_argument("--correlation-id", help="Optional request/trace correlation identifier")
	p_scan.add_argument("--json", action="store_true", help="Emit machine-readable output")

	# Profile tiers to derive latency/throughput/capacity metrics
	p_prof = sub.add_parser("tier-profile")
	p_prof.add_argument("--metrics-out", help="Optional path to write JSON metrics for all tiers")

	# Scan device hardware characteristics reported by the OS
	p_dev = sub.add_parser("devices-scan")
	p_dev.add_argument("--hardware-out", help="Optional path to write JSON hardware info for all tiers")

	# Auto-refresh caches: hardware + metrics, once or periodically
	p_auto = sub.add_parser("auto-refresh")
	p_auto.add_argument("--cache-dir", default=".cognistore", help="Directory to store cache files (hardware.json, tier_metrics.json)")
	p_auto.add_argument("--interval", type=int, default=0, help="Seconds between refresh cycles; 0 to run once and exit")

	p_policy = sub.add_parser("policy-run")
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
	# Planning only
	p_policy.add_argument("--dry-run", action="store_true", help="Plan moves but do not modify storage or catalog")
	p_policy.add_argument("--sync", action="store_true", help="Run writable work inline instead of enqueueing (development only)")
	p_policy.add_argument("--job-id", help="Optional UUID to use as the logical job ID")
	p_policy.add_argument("--correlation-id", help="Optional request/trace correlation identifier")
	p_policy.add_argument("--json", action="store_true", help="Emit machine-readable action output")

	p_worker = sub.add_parser("worker", help="Run the durable background worker")
	p_worker.add_argument("--health-host", default="127.0.0.1")
	p_worker.add_argument("--health-port", type=int, default=8081)
	p_worker.add_argument("--fetch-timeout", type=float, default=1.0)
	p_worker.add_argument("--heartbeat-interval", type=float, default=10.0)
	p_worker.add_argument("--shutdown-grace", type=float, default=30.0)
	p_worker.add_argument("--settlement-timeout", type=float, default=5.0)
	p_worker.add_argument("--max-attempts", type=int, default=7)
	p_worker.add_argument("--retry-base-delay", type=float, default=1.0)
	p_worker.add_argument("--retry-max-delay", type=float, default=30.0)
	p_worker.add_argument("--retry-jitter", type=float, default=0.2)
	p_worker.add_argument("--once", action="store_true", help="Stop after settling one delivery (primarily for tests)")

	p_redrive = sub.add_parser(
		"dead-letter-redrive",
		aliases=["job-redrive", "dlq-redrive"],
		help="Republish one immutable dead-letter entry",
	)
	p_redrive.add_argument("dead_letter_id")
	p_redrive.add_argument("--json", action="store_true", help="Emit machine-readable output")

	args = parser.parse_args(argv)
	if args.cmd in {"dead-letter-redrive", "job-redrive", "dlq-redrive"}:
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
	dry_run = bool(getattr(args, "dry_run", False))
	background_submission = (
		args.cmd in {"catalog-scan", "policy-run"}
		and not dry_run
		and not getattr(args, "sync", False)
	)
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
	if args.cmd == "worker" and args.heartbeat_interval >= args.ack_wait:
		parser.error("--heartbeat-interval must be less than --ack-wait")
	if args.cmd == "worker":
		try:
			WorkerConfig(
				fetch_timeout=args.fetch_timeout,
				heartbeat_interval=args.heartbeat_interval,
				shutdown_grace=args.shutdown_grace,
				settlement_timeout=args.settlement_timeout,
				max_attempts=args.max_attempts,
				retry_base_delay=args.retry_base_delay,
				retry_max_delay=args.retry_max_delay,
				retry_jitter=args.retry_jitter,
			)
		except ValueError as exc:
			parser.error(str(exc))
	if background_submission and args.catalog_db:
		parser.error(
			"--catalog-db configures inline work only; background jobs use the "
			"worker's --catalog-db"
		)

	policy_allowed = None
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
		# Validate the full storage plan before a writable catalog is opened.
		try:
			Mover(drivers, Catalog()).plan(args.src, args.dst, args.bucket, args.key)
		except (ValueError, FileExistsError, FileNotFoundError) as exc:
			parser.error(str(exc))

	catalog: Catalog | None
	if background_submission:
		catalog = None
	elif args.catalog_db and args.cmd == "policy-run" and dry_run:
		if not Path(args.catalog_db).expanduser().exists():
			parser.error("--catalog-db must already exist for policy-run --dry-run")
		catalog = SQLiteCatalog(args.catalog_db, read_only=True)
	elif args.catalog_db and not (args.cmd == "move" and dry_run):
		catalog = SQLiteCatalog(args.catalog_db)
	else:
		catalog = Catalog()

	if args.cmd == "worker":
		assert drivers is not None
		assert isinstance(catalog, SQLiteCatalog)
		try:
			return asyncio.run(_serve_worker(args, drivers, catalog))
		finally:
			catalog.close()

	if args.cmd == "put":
		data = Path(args.file).read_bytes()
		if driver is None:
			assert drivers is not None
			active_driver = drivers["hot"]
		else:
			active_driver = driver
		active_driver.put_object(args.bucket, args.key, data)
		return 0
	if args.cmd == "get":
		if driver is None:
			assert drivers is not None
			active_driver = drivers["hot"]
		else:
			active_driver = driver
		data = active_driver.get_object(args.bucket, args.key)
		Path(args.out).parent.mkdir(parents=True, exist_ok=True)
		Path(args.out).write_bytes(data)
		return 0
	if args.cmd == "ls":
		if driver is None:
			assert drivers is not None
			active_driver = drivers["hot"]
		else:
			active_driver = driver
		for k in active_driver.list_objects(args.bucket, prefix=args.prefix):
			print(k)
		return 0

	assert drivers is not None

	# move between tiers
	if args.cmd == "move":
		assert catalog is not None
		mv = Mover(drivers, catalog)
		if args.dry_run:
			mv.plan(args.src, args.dst, args.bucket, args.key)
			status: Literal["planned", "completed"] = "planned"
		else:
			mv.move(args.src, args.dst, args.bucket, args.key)
			status = "completed"
		action = ActionResult(
			bucket=args.bucket,
			key=args.key,
			from_tier=args.src,
			to_tier=args.dst,
			reason="manual move",
			status=status,
		)
		_render_actions([action], dry_run=args.dry_run, json_output=args.json)
		return 0

	if args.cmd == "ls-tier":
		tier = args.tier
		for k in drivers[tier].list_objects(args.bucket, prefix=args.prefix):
			print(k)
		return 0

	if args.cmd == "catalog-scan":
		if args.tier not in drivers:
			parser.error(f"unknown tier: {args.tier}")
		if background_submission:
			job = JobEnvelope.create(
				CATALOG_SCAN_JOB,
				{"tier": args.tier, "bucket": args.bucket, "prefix": args.prefix},
				job_id=args.job_id,
				correlation_id=args.correlation_id,
			)
			receipt = asyncio.run(
				_enqueue_job(
					_queue_config(
						args, client_name="cognistore-cli", one_shot=True
					),
					job,
				)
			)
			_render_enqueue(job, receipt, json_output=args.json)
			return 0

		assert catalog is not None
		scan_results = scan_catalog(
			tier=args.tier,
			bucket=args.bucket,
			prefix=args.prefix,
			driver=drivers[args.tier],
			catalog=catalog,
		)
		for result in scan_results:
			print(
				f"indexed {result.tier}:{result.bucket}/{result.key} "
				f"size={result.size}"
			)
		return 0

	if args.cmd == "tier-profile":
		# Ensure drivers available
		if not drivers:
			parser.error("--drivers is required for tier-profile")
			return 1
		profile_results = {}
		for tier, drv in drivers.items():
			# We only know PosixDriver has a base path; fall back to the driver's repr if unavailable
			base_path = None
			if isinstance(drv, PosixDriver):
				base_path = drv.base
			else:
				# Try to resolve to path-like if provided via stat or attribute
				base_path = getattr(drv, "base", None) or getattr(drv, "base_path", None)
			if not base_path:
				print(f"skip {tier}: unsupported driver for profiling", file=sys.stderr)
				continue
			m = profile_path(str(base_path))
			profile_results[tier] = m
			print(f"{tier}: read={m.seq_read_MBps:.1f} MB/s write={m.seq_write_MBps:.1f} MB/s randIOPS={m.random_read_IOPS:.0f} fbyte={m.first_byte_latency_ms:.2f} ms free={m.free_bytes/1e9:.1f}G/{m.total_bytes/1e9:.1f}G")
		if args.metrics_out:
			save_metrics_json(profile_results, args.metrics_out)
			print(f"wrote metrics -> {args.metrics_out}")
		return 0

	if args.cmd == "devices-scan":
		if not drivers:
			parser.error("--drivers is required for devices-scan")
			return 1
		info = {}
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
			print(f"{tier}: dev={di.device or '?'} base={di.base_device or '?'} type={mt} model={di.model or '?'} transport={di.transport or '?'}")
		if args.hardware_out:
			save_hardware_json(info, args.hardware_out)
			print(f"wrote hardware -> {args.hardware_out}")
		return 0

	if args.cmd == "auto-refresh":
		if not drivers:
			parser.error("--drivers is required for auto-refresh")
			return 1
		cache_dir = Path(args.cache_dir)
		cache_dir.mkdir(parents=True, exist_ok=True)
		hw_path = cache_dir / "hardware.json"
		m_path = cache_dir / "tier_metrics.json"

		def do_refresh():
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
			print(f"refreshed hardware -> {hw_path}")
			# Metrics profile
			refresh_metrics = {}
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
			print(f"refreshed metrics -> {m_path}")

		if args.interval and args.interval > 0:
			try:
				while True:
					do_refresh()
					time.sleep(args.interval)
			except KeyboardInterrupt:
				return 0
		else:
			do_refresh()
			return 0

	if args.cmd == "policy-run":
		assert policy_allowed is not None
		allowed = policy_allowed

		def _policy_info(message: str) -> None:
			print(message, file=sys.stderr if args.json else sys.stdout)

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
				print(f"warning: failed to load/derive metrics from {args.metrics_in}: {e}", file=sys.stderr)
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
				print(f"warning: failed to load/derive hardware from {args.hardware_in}: {e}", file=sys.stderr)
		effective_threshold = (
			derived_threshold if derived_threshold is not None else args.threshold
		)
		policy_threshold = (
			args.threshold if args.policy == "llm" else effective_threshold
		)
		if background_submission:
			job = JobEnvelope.create(
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
			receipt = asyncio.run(
				_enqueue_job(
					_queue_config(
						args, client_name="cognistore-cli", one_shot=True
					),
					job,
				)
			)
			_render_enqueue(job, receipt, json_output=args.json)
			return 0

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
		_render_actions(actions, dry_run=args.dry_run, json_output=args.json)
		return 0
	return 1


if __name__ == "__main__":
	raise SystemExit(main())
