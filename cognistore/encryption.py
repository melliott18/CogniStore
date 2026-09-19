"""Fail-closed transport policy and evidence for externally managed encryption.

Attestations describe deployment controls; they never claim that this process
can inspect a provider's disks. No key material belongs in this configuration.
"""

from __future__ import annotations

import json
import os
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class EncryptionConfigurationError(ValueError):
    """Encryption configuration cannot meet the selected security profile."""


_COMPONENTS = frozenset({"catalog", "queue", "storage", "runtime"})
_MODES = frozenset({"provider-managed", "encrypted-volume"})


def production_mode() -> bool:
    """Only an explicit development profile permits plaintext transports."""
    profile = os.environ.get("COGNISTORE_SECURITY_PROFILE", "production")
    if profile not in {"production", "development"}:
        raise EncryptionConfigurationError(
            "COGNISTORE_SECURITY_PROFILE must be production or development"
        )
    return profile == "production"


def require_tls_url(
    url: str, service: str, schemes: tuple[str, ...] = ("https",),
) -> None:
    if not production_mode():
        return
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme in schemes and bool(parsed.hostname)
        parsed.port
    except (TypeError, ValueError):
        valid = False
    if not valid:
        # Never interpolate an endpoint: it may contain credentials.
        raise EncryptionConfigurationError(f"{service} requires verified TLS in production")


def ca_bundle() -> str | bool:
    """Return a configured CA file or SDK certificate verification default."""
    path = os.environ.get("COGNISTORE_TLS_CA_FILE")
    if path is None:
        return True
    if not path.strip() or not Path(path).is_file():
        raise EncryptionConfigurationError("COGNISTORE_TLS_CA_FILE must be a readable CA file")
    try:
        ssl.create_default_context(cafile=path)
    except (OSError, ValueError):
        raise EncryptionConfigurationError("Unable to load the configured TLS CA file") from None
    return path


def tls_context() -> ssl.SSLContext:
    bundle = ca_bundle()
    context = ssl.create_default_context(cafile=bundle if isinstance(bundle, str) else None)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    return context


def server_tls_context(
    certfile: str | None, keyfile: str | None,
) -> ssl.SSLContext | None:
    """Validate a listener key pair before creating any service connections."""
    if not certfile and not keyfile and not production_mode():
        return None
    if not certfile or not keyfile:
        raise EncryptionConfigurationError("Production listeners require a TLS certificate and key")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(certfile, keyfile)
    except (OSError, ValueError):
        raise EncryptionConfigurationError("Unable to load the listener TLS certificate and key") from None
    return context


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _attestations() -> dict[str, dict[str, Any]]:
    path = os.environ.get("COGNISTORE_ENCRYPTION_CONFIG")
    if path is None:
        return {}
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(document, dict) or set(document) != {"version", "components"}:
            raise ValueError("invalid document")
        if type(document["version"]) is not int or document["version"] != 1:
            raise ValueError("invalid version")
        components = document["components"]
        if not isinstance(components, dict) or set(components) - _COMPONENTS:
            raise ValueError("invalid components")
        for entry in components.values():
            if not isinstance(entry, dict) or set(entry) != {
                "mode", "owner", "evidence", "expires_at", "rotation_days",
            }:
                raise ValueError("invalid attestation")
            if entry["mode"] not in _MODES:
                raise ValueError("invalid encryption mode")
            if any(not isinstance(entry[k], str) or not entry[k].strip()
                   for k in ("owner", "evidence", "expires_at")):
                raise ValueError("invalid evidence")
            if type(entry["rotation_days"]) is not int or entry["rotation_days"] < 1:
                raise ValueError("invalid rotation interval")
            expiry = datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry.utcoffset() is None:
                raise ValueError("expiry must include timezone")
        return dict(components)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError):
        raise EncryptionConfigurationError("Invalid encryption attestation configuration") from None


def _current(entry: dict[str, Any]) -> bool:
    expiry = datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00"))
    return expiry > datetime.now(timezone.utc)


def require_at_rest(component: str, mode: str | None = None) -> None:
    if component not in _COMPONENTS:
        raise EncryptionConfigurationError("Unknown encryption component")
    if mode is not None and mode != "provider-managed":
        raise EncryptionConfigurationError("Unsupported native encryption mode")
    if not production_mode() or mode == "provider-managed":
        return
    entry = _attestations().get(component)
    if entry is None or not _current(entry):
        raise EncryptionConfigurationError(
            f"Production {component} requires a current encryption-at-rest attestation "
            "in COGNISTORE_ENCRYPTION_CONFIG"
        )


def encryption_status() -> dict[str, Any]:
    """Report only allowlisted controls, never paths, evidence, owners or keys."""
    production = production_mode()
    entries = _attestations()
    ca_bundle()
    return {
        "profile": "production" if production else "development",
        "tls": {"required": production, "certificate_verification": True,
                "custom_ca": "COGNISTORE_TLS_CA_FILE" in os.environ},
        "at_rest": {
            component: {
                "mode": entry["mode"] if entry else "unattested",
                "source": "operator-attestation" if entry else "none",
                "current": bool(entry and _current(entry)),
                "rotation_days": entry["rotation_days"] if entry else None,
            }
            for component in sorted(_COMPONENTS)
            for entry in [entries.get(component)]
        },
    }


def require_verified_httpx(client_or_transport: object) -> None:
    """Require verified TLS and reject opaque transports in production.

    HTTPX 0.28.1 exposes no public inspection API for transport verification.
    Inspect its pinned transport/pool layout and fail closed if it changes.
    MockTransport is an explicit offline testing seam with no network I/O.
    """
    production = production_mode()
    import httpx

    if isinstance(client_or_transport, (httpx.Client, httpx.AsyncClient)):
        transports: list[object] = [client_or_transport._transport]
        transports.extend(
            transport for transport in client_or_transport._mounts.values()
            if transport is not None
        )
    else:
        transports = [client_or_transport]
    for transport in transports:
        if isinstance(transport, httpx.MockTransport):
            continue
        pool = getattr(transport, "_pool", None)
        context = getattr(pool, "_ssl_context", None)
        proxy_context = getattr(pool, "_proxy_ssl_context", None)
        contexts = [context] if proxy_context is None else [context, proxy_context]
        if not production and context is None:
            continue
        if any(
            not isinstance(candidate, ssl.SSLContext)
            or candidate.verify_mode != ssl.CERT_REQUIRED
            or not candidate.check_hostname
            or candidate.minimum_version < ssl.TLSVersion.TLSv1_2
            for candidate in contexts
        ):
            raise EncryptionConfigurationError(
                "HTTPX transports require certificate and hostname verification "
                "with TLS 1.2 or newer"
            )


def main() -> int:
    try:
        print(json.dumps(encryption_status(), sort_keys=True))
    except EncryptionConfigurationError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
