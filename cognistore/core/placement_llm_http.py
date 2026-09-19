"""HTTP adapter for an explicitly configured, trusted placement model service.

The endpoint accepts ``{model, prompt, schema}`` and returns the decision JSON
directly. The inference layer owns schema validation, deadlines and retries;
this adapter never retries or follows redirects itself.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx

from cognistore.encryption import require_tls_url, require_verified_httpx, tls_context

from .placement_llm import MAX_RESPONSE_BYTES, PlacementInference, TransientPlacementError

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503})
_TIMEOUT_STATUSES = frozenset({408, 504})


def _identity(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{field} must be nonempty text without control characters")
    return value


def _endpoint(value: object) -> str:
    message = "LLM endpoint must use HTTPS (or loopback HTTP), without credentials or fragments"
    if (
        not isinstance(value, str)
        or not value
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        or "#" in value
    ):
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or (port is not None and port == 0)
        ):
            raise ValueError(message)
        if parsed.scheme == "http" and host != "localhost":
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError(message)
        # Validate URL syntax without making a network request.
        httpx.URL(value)
    except (ValueError, httpx.InvalidURL):
        raise ValueError(message) from None
    require_tls_url(value, "LLM")
    return value


def _redact_credential_response(text: str, secret: str) -> str:
    """Redact decoded strings without repairing duplicate or malformed JSON."""

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError("non-finite number")

    # Do not shorten an overlong reason into an otherwise valid placement.
    marker = "[REDACTED]".ljust(len(secret), "*")
    if secret in marker:
        marker = "\u2588" * len(marker)  # API keys are ASCII, so this cannot contain the key.

    def replace(value: object) -> object:
        if isinstance(value, str):
            return value.replace(secret, marker)
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            return unique_object([(key.replace(secret, marker), replace(item))
                                  for key, item in value.items()])
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite number")
        return value

    try:
        decoded = json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
        redacted = replace(decoded)
        if isinstance(decoded, dict) and isinstance(redacted, dict):
            if any(decoded.get(field) != redacted.get(field) for field in ("action", "dst_tier")):
                raise ValueError("redaction changes routing")
        if redacted == decoded:
            return text
        result = json.dumps(redacted, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if len(result.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ValueError("response too large")
        return result
    except (ValueError, RecursionError):
        raise RuntimeError("LLM response cannot be safely redacted") from None


class HTTPPlacementProvider:
    """Provider-neutral JSON-over-HTTP placement adapter.

    ``api_key`` is sent only in the Authorization header. ``transport`` is a
    dependency-injection seam for offline tests; production uses HTTPTransport
    with transport retries disabled. The endpoint must be deployment-controlled,
    never supplied by an object or by a placement request.
    """

    provider_id = "cognistore-http-placement"
    version = "1"

    def __init__(
        self,
        endpoint: str,
        *,
        model: str,
        model_version: str = "1",
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._endpoint = _endpoint(endpoint)
        if transport is not None:
            require_verified_httpx(transport)
        self.model = _identity(model, "LLM model")
        self.model_version = _identity(model_version, "LLM model version")
        if api_key is not None and (
            not isinstance(api_key, str)
            or not api_key
            or any(ord(char) < 33 or ord(char) > 126 for char in api_key)
        ):
            raise ValueError("LLM API key must be nonempty ASCII text without whitespace")
        self._api_key = api_key
        self._transport = transport

    def complete(self, prompt: str, schema: dict, timeout_seconds: float) -> str:
        """Return bounded UTF-8 response text, with sanitized failure messages."""

        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("LLM timeout must be a positive finite number")
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            transport = self._transport or httpx.HTTPTransport(
                retries=0, trust_env=False, verify=tls_context(),
            )
            with httpx.Client(
                transport=transport,
                timeout=timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                with client.stream(
                    "POST",
                    self._endpoint,
                    headers=headers,
                    json={"model": self.model, "prompt": prompt, "schema": schema},
                ) as response:
                    if response.status_code in _TIMEOUT_STATUSES:
                        raise TimeoutError("LLM service timed out")
                    if response.status_code in _RETRYABLE_STATUSES:
                        raise TransientPlacementError("LLM service temporarily unavailable")
                    if not 200 <= response.status_code < 300:
                        raise RuntimeError("LLM service rejected the request")
                    # Refuse compressed bodies so decoding cannot allocate an
                    # unbounded expansion before applying the byte limit.
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise RuntimeError("LLM service returned unsupported content encoding")
                    length = response.headers.get("content-length")
                    if length is not None:
                        try:
                            declared_length = int(length)
                        except ValueError:
                            raise RuntimeError("LLM service returned invalid content length") from None
                        if not 0 <= declared_length <= MAX_RESPONSE_BYTES:
                            raise RuntimeError("LLM response exceeds the byte limit")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise RuntimeError("LLM response exceeds the byte limit")
                        body.extend(chunk)
                    try:
                        text = body.decode("utf-8", errors="strict")
                    except UnicodeDecodeError:
                        raise RuntimeError("LLM response is not UTF-8") from None
                    if self._api_key is not None:
                        text = _redact_credential_response(text, self._api_key)
                    return text
        except httpx.TimeoutException:
            raise TimeoutError("LLM service timed out") from None
        except httpx.ConnectError:
            raise TransientPlacementError("LLM service connection failed") from None
        except httpx.HTTPError:
            raise RuntimeError("LLM service transport failed") from None


def placement_inference_from_env(
    environ: Mapping[str, str] | None = None,
) -> PlacementInference:
    """Configure inference from deployment environment, never from policy input.

    An absent endpoint returns an unavailable inference path that stays in place.
    A configured endpoint requires a model. Invalid configuration fails early
    without including environment values, URLs or credentials in errors.
    """

    config = os.environ if environ is None else environ
    endpoint = config.get("COGNISTORE_LLM_ENDPOINT")
    if endpoint is None:
        return PlacementInference()
    try:
        timeout_seconds = float(config.get("COGNISTORE_LLM_TIMEOUT_SECONDS", "10"))
        max_attempts = int(config.get("COGNISTORE_LLM_MAX_ATTEMPTS", "2"))
        provider = HTTPPlacementProvider(
            endpoint,
            model=config.get("COGNISTORE_LLM_MODEL", ""),
            model_version=config.get("COGNISTORE_LLM_MODEL_VERSION", "1"),
            api_key=config.get("COGNISTORE_LLM_API_KEY"),
        )
        return PlacementInference(
            provider,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Invalid LLM deployment configuration") from None
