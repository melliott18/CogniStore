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

	p_policy = sub.add_parser("policy-run")
	p_policy.add_argument("bucket")
	p_policy.add_argument("--prefix", default="")
	p_policy.add_argument("--threshold", type=int, default=1024*1024, help="Size threshold for policies")
	p_policy.add_argument("--policy", choices=["simple", "llm", "content"], default="simple")
	p_policy.add_argument("--allowed-tiers", default="hot,warm", help="Comma-separated list of allowed tiers")
	p_policy.add_argument("--llm-threshold", type=int, help="Optional override threshold when using --policy llm")
	# Content-aware options
	p_policy.add_argument("--hot-name", action="append", help="Glob pattern(s) for keys that should go to hot")
	p_policy.add_argument("--warm-name", action="append", help="Glob pattern(s) for keys that should go to warm")
	p_policy.add_argument("--hot-mime", action="append", help="MIME prefix(es) that should go to hot, e.g. text/ or image/")
	p_policy.add_argument("--warm-mime", action="append", help="MIME prefix(es) that should go to warm, e.g. application/zip")

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

	if args.cmd == "policy-run":
		allowed = tuple([t.strip() for t in args.allowed_tiers.split(",") if t.strip()])
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
				size_threshold=args.threshold,
				allowed_tiers=allowed,
				hot_name_patterns=args.hot_name or [],
				warm_name_patterns=args.warm_name or [],
				hot_mime_prefixes=args.hot_mime or [],
				warm_mime_prefixes=args.warm_mime or [],
			)
		else:
			policy = SimplePolicy(size_threshold=args.threshold)
		mv = Mover(drivers, catalog)
		runner = PolicyRunner(catalog, drivers, mv, policy)
		actions = runner.run_once(args.bucket, prefix=args.prefix)
		for a in actions:
			print(f"moved {a.bucket}/{a.key} {a.from_tier}->{a.to_tier} : {a.reason}")
		print(f"actions={len(actions)}")
		return 0
	return 1


if __name__ == "__main__":
	raise SystemExit(main())

