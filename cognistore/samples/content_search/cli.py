"""Load, verify, and serve the packaged content-search sample."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from cognistore.api import create_app
from cognistore.core.embedding_index import EmbeddingBackendUnsupportedError
from cognistore.db import SQLCatalog, open_catalog
from cognistore.drivers.driver_loader import load_drivers
from cognistore.observability import configure_observability

from .manifest import SampleManifestError, read_sample_corpus
from .runtime import ContentSearchRuntime, SampleLoadError


def _runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--drivers",
        default=os.environ.get("COGNISTORE_DRIVERS"),
        help="driver YAML path (or COGNISTORE_DRIVERS)",
    )
    parser.add_argument(
        "--catalog-db",
        default=os.environ.get("COGNISTORE_CATALOG_DB"),
        help="PostgreSQL DSN (or COGNISTORE_CATALOG_DB)",
    )
    parser.add_argument(
        "--keyword-index",
        type=Path,
        default=Path(
            os.environ.get(
                "COGNISTORE_KEYWORD_INDEX",
                ".cognistore-sample/keyword-index",
            )
        ),
        help="persistent Tantivy directory (or COGNISTORE_KEYWORD_INDEX)",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser(
        "verify",
        help="verify packaged manifests, checksums, and object payloads",
    )
    verify.set_defaults(action=_verify)

    load = subparsers.add_parser(
        "load",
        help="ingest, extract, keyword-index, and embed the sample corpus",
    )
    _runtime_arguments(load)
    load.add_argument(
        "--no-overwrite",
        action="store_true",
        help="fail rather than idempotently replace existing sample objects",
    )
    load.set_defaults(action=_load)

    serve = subparsers.add_parser(
        "serve",
        help="serve the search UI and fully composed sample API",
    )
    _runtime_arguments(serve)
    serve.add_argument(
        "--load",
        action="store_true",
        help="load or refresh the corpus before starting the server",
    )
    serve.add_argument(
        "--host",
        default=os.environ.get("COGNISTORE_API_HOST", "127.0.0.1"),
    )
    serve.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("COGNISTORE_API_PORT", "8080")),
    )
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(action=_serve)
    return parser


def _required_runtime_values(args: argparse.Namespace) -> tuple[str, str]:
    if not args.drivers:
        raise ValueError("--drivers or COGNISTORE_DRIVERS is required")
    if not args.catalog_db:
        raise ValueError("--catalog-db or COGNISTORE_CATALOG_DB is required")
    return args.drivers, args.catalog_db


def _open_runtime(
    args: argparse.Namespace,
) -> tuple[SQLCatalog, ContentSearchRuntime]:
    driver_path, catalog_locator = _required_runtime_values(args)
    drivers = load_drivers(driver_path)
    catalog = open_catalog(catalog_locator)
    if not isinstance(catalog, SQLCatalog):  # pragma: no cover - factory is SQL-backed
        close = getattr(catalog, "close", None)
        if callable(close):
            close()
        raise TypeError("the content-search sample requires a SQL catalog")
    try:
        runtime = ContentSearchRuntime(
            catalog,
            drivers,
            keyword_index_path=args.keyword_index,
        )
    except BaseException:
        catalog.close()
        raise
    return catalog, runtime


def _verify(_args: argparse.Namespace) -> int:
    corpus = read_sample_corpus()
    duplicate_digests = {
        item.sha256
        for item in corpus.manifest.objects
        if sum(
            candidate.sha256 == item.sha256
            for candidate in corpus.manifest.objects
        )
        > 1
    }
    print(
        json.dumps(
            {
                "schema_version": corpus.manifest.schema_version,
                "corpus": corpus.manifest.name,
                "license": corpus.manifest.license,
                "objects": len(corpus.manifest.objects),
                "duplicate_content_groups": len(duplicate_digests),
                "queries": sorted(corpus.manifest.queries),
                "status": "verified",
            },
            sort_keys=True,
        )
    )
    return 0


def _load(args: argparse.Namespace) -> int:
    catalog, runtime = _open_runtime(args)
    try:
        with runtime:
            report = runtime.load(overwrite=not args.no_overwrite)
        print(json.dumps(report.to_dict(), sort_keys=True))
        return 0
    finally:
        catalog.close()


def _serve(args: argparse.Namespace) -> int:
    configure_observability()
    if not 1 <= args.port <= 65_535:
        raise ValueError("--port must be between 1 and 65535")
    catalog, runtime = _open_runtime(args)
    try:
        with runtime:
            if args.load:
                report = runtime.load()
                print(json.dumps(report.to_dict(), sort_keys=True))
            uvicorn.run(
                create_app(runtime.create_gateway()),
                host=args.host,
                port=args.port,
                log_level=args.log_level,
                access_log=False,
                log_config=(
                    None if os.environ.get("COGNISTORE_LOG_FORMAT", "").lower() == "json"
                    else uvicorn.config.LOGGING_CONFIG
                ),
            )
        return 0
    finally:
        catalog.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.action(args))
    except (
        EmbeddingBackendUnsupportedError,
        SampleManifestError,
        SampleLoadError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"content-search sample: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
