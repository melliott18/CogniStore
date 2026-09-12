"""Runtime entry point for the standalone CogniStore API process."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

import uvicorn

from cognistore.auth.jwt import JWTAuthConfig
from cognistore.db import open_catalog
from cognistore.drivers.driver_loader import load_drivers
from cognistore.jobs.nats_queue import NatsJetStreamConfig, NatsJetStreamQueue
from cognistore.observability import configure_observability
from cognistore.policy_feature_runtime import load_access_config, load_policy_feature_loader

from .app import create_app
from .gateway import CogniStoreGateway


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the CogniStore REST API")
    parser.add_argument(
        "--drivers",
        default=os.environ.get("COGNISTORE_DRIVERS"),
        help="driver YAML path (or COGNISTORE_DRIVERS)",
    )
    parser.add_argument(
        "--catalog-db",
        default=os.environ.get("COGNISTORE_CATALOG_DB"),
        help="SQLite path or PostgreSQL DSN (or COGNISTORE_CATALOG_DB)",
    )
    parser.add_argument(
        "--nats-url",
        action="append",
        help="NATS URL; repeat for a cluster (defaults to COGNISTORE_NATS_URL)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("COGNISTORE_API_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        default=int(os.environ.get("COGNISTORE_API_PORT", "8080")),
        type=int,
    )
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--auth-issuer", default=os.environ.get("COGNISTORE_AUTH_ISSUER"),
        help="trusted HTTPS token issuer (or COGNISTORE_AUTH_ISSUER)",
    )
    parser.add_argument(
        "--auth-audience", default=os.environ.get("COGNISTORE_AUTH_AUDIENCE"),
        help="required API audience (or COGNISTORE_AUTH_AUDIENCE)",
    )
    parser.add_argument(
        "--auth-jwks-uri", default=os.environ.get("COGNISTORE_AUTH_JWKS_URI"),
        help="trusted HTTPS JWKS endpoint; otherwise use OIDC discovery",
    )
    parser.add_argument(
        "--auth-algorithm", action="append",
        help="allowed asymmetric JWT algorithm; repeat (default RS256 or COGNISTORE_AUTH_ALGORITHMS)",
    )
    parser.add_argument(
        "--auth-required-claim", action="append",
        help="required claim; repeat (default sub,exp,iat or COGNISTORE_AUTH_REQUIRED_CLAIMS)",
    )
    return parser


def _nats_servers(values: list[str] | None) -> tuple[str, ...]:
    if values:
        return tuple(values)
    configured = os.environ.get("COGNISTORE_NATS_URL", "nats://127.0.0.1:4222")
    return tuple(item.strip() for item in configured.split(",") if item.strip())


def _auth_config(args: argparse.Namespace) -> JWTAuthConfig | None:
    algorithms_env = os.environ.get("COGNISTORE_AUTH_ALGORITHMS")
    claims_env = os.environ.get("COGNISTORE_AUTH_REQUIRED_CLAIMS")
    configured = (
        args.auth_issuer, args.auth_audience, args.auth_jwks_uri,
        args.auth_algorithm, args.auth_required_claim, algorithms_env, claims_env,
    )
    if all(value is None for value in configured):
        return None
    if not args.auth_issuer or not args.auth_audience:
        raise SystemExit("JWT authentication requires both --auth-issuer and --auth-audience")
    algorithms = tuple(args.auth_algorithm) if args.auth_algorithm is not None else (
        tuple(value.strip() for value in algorithms_env.split(","))
        if algorithms_env is not None else ("RS256",)
    )
    required_claims = tuple(args.auth_required_claim) if args.auth_required_claim is not None else (
        tuple(value.strip() for value in claims_env.split(","))
        if claims_env is not None else ("sub", "exp", "iat")
    )
    try:
        return JWTAuthConfig(
            issuer=args.auth_issuer,
            audience=args.auth_audience,
            jwks_uri=args.auth_jwks_uri,
            algorithms=algorithms,
            required_claims=required_claims,
        )
    except ValueError:
        raise SystemExit("Invalid JWT authentication configuration; check issuer, audience and options") from None


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_observability()
    if not args.drivers:
        raise SystemExit("--drivers or COGNISTORE_DRIVERS is required")
    if not args.catalog_db:
        raise SystemExit("--catalog-db or COGNISTORE_CATALOG_DB is required")
    if not 1 <= args.port <= 65_535:
        raise SystemExit("--port must be between 1 and 65535")

    authentication = _auth_config(args)
    drivers = load_drivers(args.drivers)
    catalog = open_catalog(args.catalog_db)
    try:
        feature_loader = load_policy_feature_loader(args.drivers, catalog)
        queue = NatsJetStreamQueue(
            NatsJetStreamConfig(
                servers=_nats_servers(args.nats_url),
                stream=os.environ.get("COGNISTORE_JOB_STREAM", "COGNISTORE_JOBS"),
                subject=os.environ.get("COGNISTORE_JOB_SUBJECT", "cognistore.jobs"),
                consumer=os.environ.get("COGNISTORE_JOB_CONSUMER", "cognistore-workers"),
                client_name="cognistore-api",
            ),
            consume=False,
        )
        gateway = CogniStoreGateway(
            catalog,
            drivers,
            queue=queue,
            manage_queue=True,
            feature_loader=feature_loader,
            access_config=load_access_config(args.drivers),
        )
        app = create_app(gateway, authentication=authentication)
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            access_log=False,
            log_config=(
                None if os.environ.get("COGNISTORE_LOG_FORMAT", "").lower() == "json"
                else uvicorn.config.LOGGING_CONFIG
            ),
        )
    finally:
        close = getattr(catalog, "close", None)
        if callable(close):
            close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
