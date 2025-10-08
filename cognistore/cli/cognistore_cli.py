from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cognistore.core.catalog import Catalog
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.core.mover import Mover
from cognistore.drivers.driver_loader import load_drivers
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.core.policy import SimplePolicy, LLMPolicy, ContentAwarePolicy
from cognistore.core.policy_runner import PolicyRunner
from cognistore.core.indexer import Indexer
from cognistore.utils.tier_profiler import profile_path, save_metrics_json, load_metrics_json


def main(argv=None):
	parser = argparse.ArgumentParser(prog="cognistore", description="CogniStore CLI")
	parser.add_argument("--base", help="Base path for POSIX storage (used when --drivers is not provided)")
	parser.add_argument("--drivers", help="Path to drivers.yaml to enable multi-tier operations")
	parser.add_argument("--catalog-db", help="Path to SQLite catalog DB; if omitted uses in-memory catalog")
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

	p_lst = sub.add_parser("ls-tier")
	p_lst.add_argument("tier")
	p_lst.add_argument("bucket")
	p_lst.add_argument("--prefix", default="")

	p_scan = sub.add_parser("catalog-scan")
	p_scan.add_argument("tier", help="Tier to scan (e.g., hot)")
	p_scan.add_argument("bucket")
	p_scan.add_argument("--prefix", default="")

	# Profile tiers to derive latency/throughput/capacity metrics
	p_prof = sub.add_parser("tier-profile")
	p_prof.add_argument("--metrics-out", help="Optional path to write JSON metrics for all tiers")

	p_policy = sub.add_parser("policy-run")
	p_policy.add_argument("bucket")
	p_policy.add_argument("--prefix", default="")
	p_policy.add_argument("--threshold", type=int, default=1024*1024, help="Size threshold for policies")
	p_policy.add_argument("--policy", choices=["simple", "llm", "content"], default="simple")
	p_policy.add_argument("--allowed-tiers", default="hot,warm", help="Comma-separated list of allowed tiers")
	p_policy.add_argument("--llm-threshold", type=int, help="Optional override threshold when using --policy llm")
	p_policy.add_argument("--metrics-in", help="Optional JSON metrics from tier-profile to inform policy")
	# Content-aware options
	p_policy.add_argument("--hot-name", action="append", help="Glob pattern(s) for keys that should go to hot")
	p_policy.add_argument("--warm-name", action="append", help="Glob pattern(s) for keys that should go to warm")
	p_policy.add_argument("--hot-mime", action="append", help="MIME prefix(es) that should go to hot, e.g. text/ or image/")
	p_policy.add_argument("--warm-mime", action="append", help="MIME prefix(es) that should go to warm, e.g. application/zip")
	p_policy.add_argument("--cold-name", action="append", help="Glob pattern(s) for keys that should go to cold")
	p_policy.add_argument("--cold-mime", action="append", help="MIME prefix(es) that should go to cold, e.g. application/x-tar")
	# Planning only
	p_policy.add_argument("--dry-run", action="store_true", help="Plan moves but do not modify storage or catalog")

	args = parser.parse_args(argv)
	catalog = SQLiteCatalog(args.catalog_db) if args.catalog_db else Catalog()
	driver = None
	drivers = None
	if args.drivers:
		drivers = load_drivers(args.drivers)
	else:
		if not args.base:
			parser.error("either --drivers or --base is required")
		driver = PosixDriver(args.base)

	if args.cmd == "put":
		data = Path(args.file).read_bytes()
		(driver or drivers["hot"]).put_object(args.bucket, args.key, data)
		return 0
	if args.cmd == "get":
		data = (driver or drivers["hot"]).get_object(args.bucket, args.key)
		Path(args.out).parent.mkdir(parents=True, exist_ok=True)
		Path(args.out).write_bytes(data)
		return 0
	if args.cmd == "ls":
		for k in (driver or drivers["hot"]).list_objects(args.bucket, prefix=args.prefix):
			print(k)
		return 0

	# Multi-tier ops require drivers
	if not drivers:
		parser.error("--drivers is required for tier operations")

	# move between tiers
	if args.cmd == "move":
		mv = Mover(drivers, catalog)
		mv.move(args.src, args.dst, args.bucket, args.key)
		return 0

	if args.cmd == "ls-tier":
		tier = args.tier
		for k in drivers[tier].list_objects(args.bucket, prefix=args.prefix):
			print(k)
		return 0

	if args.cmd == "catalog-scan":
		tier = args.tier
		drv = drivers[tier]
		indexer = Indexer()
		for key in drv.list_objects(args.bucket, prefix=args.prefix):
			st = drv.stat_object(args.bucket, key)
			# Fetch a small sample for indexing
			data = drv.get_object(args.bucket, key, range=f"bytes=0-{min(max(st.get('size', 0)-1, 0), 1024*1024)-1}" if st.get("size", 0) else None)
			ix = indexer.index_bytes(data, filename=key)
			catalog.upsert(
				args.bucket,
				key,
				size=st.get("size", 0),
				tier=tier,
				metadata={
					"path": st.get("path"),
					"sha256": ix.sha256,
					"mime": ix.mime,
					"sample_len": len(ix.sample),
				},
			)
			print(f"indexed {tier}:{args.bucket}/{key} size={st.get('size')}")
		return 0

	if args.cmd == "tier-profile":
		# Ensure drivers available
		if not drivers:
			parser.error("--drivers is required for tier-profile")
			return 1
		results = {}
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
			results[tier] = m
			print(f"{tier}: read={m.seq_read_MBps:.1f} MB/s write={m.seq_write_MBps:.1f} MB/s randIOPS={m.random_read_IOPS:.0f} fbyte={m.first_byte_latency_ms:.2f} ms free={m.free_bytes/1e9:.1f}G/{m.total_bytes/1e9:.1f}G")
		if args.metrics_out:
			save_metrics_json(results, args.metrics_out)
			print(f"wrote metrics -> {args.metrics_out}")
		return 0

	if args.cmd == "policy-run":
		allowed = tuple([t.strip() for t in args.allowed_tiers.split(",") if t.strip()])
		# Optionally derive a data-driven size threshold from measured tier metrics
		derived_threshold = None
		if args.metrics_in:
			try:
				metrics = load_metrics_json(args.metrics_in)
				hot = metrics.get("hot")
				warm = metrics.get("warm")
				if hot and warm:
					# Convert to seconds and bytes/sec
					Lh = max(hot.first_byte_latency_ms, 0.01) / 1000.0
					Lw = max(warm.first_byte_latency_ms, 0.01) / 1000.0
					Bh = max(hot.seq_read_MBps, 0.1) * 1024 * 1024
					Bw = max(warm.seq_read_MBps, 0.1) * 1024 * 1024
					denom = (1.0 / Bw) - (1.0 / Bh)
					if denom > 0:
						bytes_switch = int((Lw - Lh) / denom)
						# Clamp to a reasonable range [64KiB, 1GiB]
						bytes_switch = max(64 * 1024, min(bytes_switch, 1024 * 1024 * 1024))
						derived_threshold = bytes_switch
						print(f"[policy] derived hot/warm threshold from metrics: {bytes_switch/1024/1024:.2f} MiB")
			except Exception as e:
				print(f"warning: failed to load/derive metrics from {args.metrics_in}: {e}", file=sys.stderr)
		if args.policy == "llm":
			th = args.llm_threshold if args.llm_threshold is not None else args.threshold
			# Minimal provider that mimics an LLM decision based on threshold
			class _ThresholdProvider:
				def decide(self, inputs: dict) -> dict:
					size = int(inputs.get("size", 0))
					current = inputs.get("current_tier")
					if size <= th and "hot" in allowed and current != "hot":
						return {"action": "move", "dst_tier": "hot", "reason": f"<= {th} bytes"}
					if size > th and "warm" in allowed and current != "warm":
						return {"action": "move", "dst_tier": "warm", "reason": f"> {th} bytes"}
					return {"action": "stay", "reason": "already optimal"}

			policy = LLMPolicy(provider=_ThresholdProvider(), allowed_tiers=allowed)
		elif args.policy == "content":
			policy = ContentAwarePolicy(
				size_threshold=(derived_threshold or args.threshold),
				allowed_tiers=allowed,
				hot_name_patterns=args.hot_name or [],
				warm_name_patterns=args.warm_name or [],
				hot_mime_prefixes=args.hot_mime or [],
				warm_mime_prefixes=args.warm_mime or [],
			)
			# Attach cold rules dynamically if present
			if hasattr(policy, "cold_name_patterns"):
				policy.cold_name_patterns = [p for p in (args.cold_name or []) if p]
			if hasattr(policy, "cold_mime_prefixes"):
				policy.cold_mime_prefixes = [m for m in (args.cold_mime or []) if m]
			# Placeholder: in future, use metrics-in to adjust thresholds/hints
		else:
			policy = SimplePolicy(size_threshold=(derived_threshold or args.threshold))
		mv = Mover(drivers, catalog)
		runner = PolicyRunner(catalog, drivers, mv, policy)
		actions = runner.run_once(args.bucket, prefix=args.prefix, dry_run=args.dry_run)
		for a in actions:
			print(f"moved {a.bucket}/{a.key} {a.from_tier}->{a.to_tier} : {a.reason}")
		print(f"actions={len(actions)}")
		return 0
	return 1


if __name__ == "__main__":
	raise SystemExit(main())

