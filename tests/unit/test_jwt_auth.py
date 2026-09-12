from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from cognistore.auth.jwt import AuthenticationError, JWTAuthConfig, JWTAuthenticator
from cognistore.auth.principal import Principal

ISSUER = "https://identity.example/tenant"
JWKS_URI = ISSUER + "/keys"
AUDIENCE = "cognistore-api"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def rotated_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key, kid: str = "first", algorithm: str = "RS256") -> dict[str, Any]:
    implementation = jwt.algorithms.get_default_algorithms()[algorithm]
    document = json.loads(implementation.to_jwk(key.public_key()))
    return {**document, "kid": kid, "alg": algorithm, "use": "sig"}


def _claims(**changes) -> dict[str, Any]:
    return {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "alice",
        "iat": int(time.time()) - 1,
        "exp": int(time.time()) + 300,
        **changes,
    }


def _token(key, *, kid="first", algorithm="RS256", claims=None, headers=None) -> str:
    return jwt.encode(
        _claims() if claims is None else claims,
        key,
        algorithm=algorithm,
        headers={"kid": kid, **(headers or {})},
    )


class _Provider:
    def __init__(self, key, **config):
        self.now = 1000.0
        self.keys = [_jwk(key)]
        self.requests: list[str] = []
        self.discovery: Any = {"issuer": ISSUER, "jwks_uri": JWKS_URI}
        self.failure: int | None = None
        self.response: httpx.Response | None = None
        self.client = httpx.Client(
            transport=httpx.MockTransport(self.handle), follow_redirects=True
        )
        self.auth = JWTAuthenticator(
            JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, leeway_seconds=0, **config),
            http_client=self.client,
            clock=lambda: self.now,
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if self.failure:
            return httpx.Response(self.failure)
        if str(request.url) == ISSUER + "/.well-known/openid-configuration":
            return httpx.Response(200, json=self.discovery)
        assert str(request.url) == JWKS_URI
        return (
            self.response
            if self.response is not None
            else httpx.Response(200, json={"keys": self.keys})
        )

    @property
    def key_requests(self) -> int:
        return self.requests.count(JWKS_URI)


def test_valid_user_and_service_principals_are_token_free_and_reuse_public_keys(signing_key):
    provider = _Provider(signing_key)
    user_token = _token(signing_key, claims=_claims(email="private@example.com", groups=["admin"]))
    user = provider.auth.authenticate(user_token)
    assert user == Principal(issuer=ISSUER, subject="alice")
    assert user.actor_type == "authenticated"
    service = provider.auth.authenticate(
        _token(signing_key, claims=_claims(sub="service:scanner", client_id="scanner"))
    )
    assert service == Principal(issuer=ISSUER, subject="service:scanner", client_id="scanner")
    assert set(vars(service)) == {"issuer", "subject", "client_id"}
    assert provider.key_requests == 1
    assert len(provider.requests) == 2
    assert all(user_token not in repr(value) for value in vars(provider.auth).values())


def test_explicit_jwks_uri_skips_discovery_and_azp_supplies_client(signing_key):
    provider = _Provider(signing_key, jwks_uri=JWKS_URI)
    principal = provider.auth.authenticate(_token(signing_key, claims=_claims(azp="client")))
    assert principal.client_id == "client"
    assert provider.requests == [JWKS_URI]


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://other.example"},
        {"aud": "other-api"},
        {"aud": ["other-api"]},
        {"exp": 1},
        {"iat": 9_999_999_999},
        {"nbf": 9_999_999_999},
        {"sub": ""},
        {"sub": "   "},
        {"sub": 123},
        {"sub": "alice\x00"},
        {"client_id": ""},
        {"client_id": None},
        {"client_id": ["client"]},
        {"azp": False},
        {"client_id": "valid", "azp": ""},
    ],
)
def test_invalid_claims_fail_closed_with_uniform_error(signing_key, changes):
    provider = _Provider(signing_key)
    credential = _token(signing_key, claims=_claims(**changes))
    with pytest.raises(AuthenticationError, match="^Invalid bearer token$") as failure:
        provider.auth.authenticate(credential)
    assert credential not in str(failure.value)
    assert "alice" not in str(failure.value)


@pytest.mark.parametrize("claim", ["iss", "aud", "sub", "exp", "iat"])
def test_required_claims_cannot_be_missing(signing_key, claim):
    provider = _Provider(signing_key)
    claims = _claims()
    claims.pop(claim)
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(signing_key, claims=claims))


def test_required_claim_policy_can_add_claims_but_cannot_remove_security_baseline(signing_key):
    provider = _Provider(signing_key, required_claims=("tenant",))
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(signing_key))
    claims = _claims(tenant="one")
    claims.pop("iat")
    assert provider.auth.authenticate(_token(signing_key, claims=claims)).subject == "alice"
    claims.pop("exp")
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(signing_key, claims=claims))


@pytest.mark.parametrize("claim", ["exp", "iat", "nbf"])
@pytest.mark.parametrize("value", [True, False, "1", None, float("nan"), float("inf"), []])
def test_numeric_dates_must_be_finite_json_numbers(signing_key, claim, value):
    provider = _Provider(signing_key)
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(signing_key, claims=_claims(**{claim: value})))


def test_fractional_numeric_dates_and_audience_list_are_valid(signing_key):
    provider = _Provider(signing_key)
    principal = provider.auth.authenticate(
        _token(signing_key, claims=_claims(exp=time.time() + 120.5, aud=["another", AUDIENCE]))
    )
    assert principal.subject == "alice"


def test_wrong_signature_is_rejected_without_refreshing_known_key(signing_key, rotated_key):
    provider = _Provider(signing_key)
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(rotated_key))
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(rotated_key))
    assert provider.key_requests == 1


@pytest.mark.parametrize(
    "algorithm,key", [("none", None), ("HS256", "s" * 64), ("HS512", "s" * 64)]
)
def test_none_and_symmetric_algorithms_are_rejected_before_fetch(signing_key, algorithm, key):
    provider = _Provider(signing_key)
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(key, algorithm=algorithm))
    assert provider.requests == []


@pytest.mark.parametrize(
    "headers",
    [{"kid": None}, {"kid": ""}, {"kid": "x" * 257}, {"alg": "RS512"}, {"crit": ["custom"]}],
)
def test_missing_key_id_and_unsupported_headers_are_rejected(signing_key, headers):
    provider = _Provider(signing_key)
    token = _token(signing_key)
    header = jwt.get_unverified_header(token) | headers
    parts = token.split(".")
    parts[0] = jwt.utils.base64url_encode(json.dumps(header).encode()).decode()
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(".".join(parts))
    assert provider.requests == []


def test_token_supplied_key_urls_and_embedded_key_are_never_trusted(signing_key, rotated_key):
    provider = _Provider(signing_key)
    headers = {
        "jku": "https://attacker.example/keys",
        "x5u": "http://localhost/secret",
        "jwk": _jwk(rotated_key),
    }
    assert provider.auth.authenticate(_token(signing_key, headers=headers)).subject == "alice"
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(rotated_key, headers=headers))
    assert set(provider.requests) == {JWKS_URI, ISSUER + "/.well-known/openid-configuration"}


def test_rotation_is_loaded_immediately_without_restart_and_misses_are_throttled(
    signing_key, rotated_key
):
    provider = _Provider(signing_key)
    assert provider.auth.authenticate(_token(signing_key)).subject == "alice"
    provider.keys = [_jwk(rotated_key, kid="second")]
    assert provider.auth.authenticate(_token(rotated_key, kid="second")).subject == "alice"
    assert provider.key_requests == 2
    for kid in ("attacker-1", "attacker-2", "first"):
        with pytest.raises(AuthenticationError):
            provider.auth.authenticate(_token(signing_key, kid=kid))
    assert provider.key_requests == 2
    provider.now += 30
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(signing_key, kid="attacker-3"))
    assert provider.key_requests == 3


def test_first_unknown_key_does_not_allow_repeated_refreshes(signing_key):
    provider = _Provider(signing_key)
    for kid in ("missing-1", "missing-2", "missing-3"):
        with pytest.raises(AuthenticationError):
            provider.auth.authenticate(_token(signing_key, kid=kid))
    assert provider.key_requests == 1
    assert provider.auth.authenticate(_token(signing_key)).subject == "alice"


def test_expired_cache_fails_closed_during_outage_and_recovers(signing_key):
    provider = _Provider(signing_key)
    token = _token(signing_key)
    provider.auth.authenticate(token)
    provider.now += 300
    provider.failure = 503
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(token)
    requests = len(provider.requests)
    for _ in range(3):
        with pytest.raises(AuthenticationError):
            provider.auth.authenticate(token)
    assert len(provider.requests) == requests
    provider.failure = None
    provider.now += 30
    assert provider.auth.authenticate(token).subject == "alice"


def test_fresh_key_survives_unknown_key_refresh_outage_until_expiry(signing_key):
    provider = _Provider(signing_key)
    token = _token(signing_key)
    provider.auth.authenticate(token)
    provider.failure = 503
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(signing_key, kid="missing"))
    assert provider.auth.authenticate(token).subject == "alice"
    provider.now += 300
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(token)


def test_parallel_validation_fetches_once_and_unknown_ids_cannot_amplify_requests(signing_key):
    provider = _Provider(signing_key)
    token = _token(signing_key)
    with ThreadPoolExecutor(max_workers=8) as pool:
        principals = list(pool.map(provider.auth.authenticate, [token] * 16))
    assert all(principal.subject == "alice" for principal in principals)
    assert provider.key_requests == 1
    unknown = _token(signing_key, kid="missing")

    def rejected(_):
        with pytest.raises(AuthenticationError):
            provider.auth.authenticate(unknown)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(rejected, range(16)))
    assert provider.key_requests == 2


@pytest.mark.parametrize(
    "discovery",
    [
        {"issuer": "https://other.example", "jwks_uri": JWKS_URI},
        {"issuer": ISSUER + "/", "jwks_uri": JWKS_URI},
        {"issuer": ISSUER},
        {"issuer": ISSUER, "jwks_uri": "http://identity.example/keys"},
        {"issuer": ISSUER, "jwks_uri": "https://user:secret@identity.example/keys"},
        {"issuer": ISSUER, "jwks_uri": "https://identity.example/keys#fragment"},
        [],
    ],
)
def test_discovery_must_match_configured_issuer_and_safe_jwks_uri(signing_key, discovery):
    provider = _Provider(signing_key)
    provider.discovery = discovery
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(signing_key))
    assert provider.key_requests == 0


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 404, 500])
def test_redirects_and_http_errors_fail_closed(signing_key, status):
    provider = _Provider(signing_key, jwks_uri=JWKS_URI)
    provider.response = httpx.Response(
        status, headers={"Location": "https://attacker.example/keys"}
    )
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(signing_key))
    assert provider.requests == [JWKS_URI]


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://identity.example",
        "ftp://identity.example",
        "https://",
        "/relative",
        "https://u:p@identity.example",
        "https://identity.example#fragment",
        "https://identity.example:0",
        "https://identity.example:70000",
        "https://identity.example\\@attacker.example",
        "https://identity.example/\nkeys",
        "https://%65xample.com",
    ],
)
def test_unsafe_configured_urls_are_rejected(url):
    with pytest.raises(ValueError):
        JWTAuthConfig(issuer=url, audience=AUDIENCE)
    with pytest.raises(ValueError):
        JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, jwks_uri=url)


def test_issuer_query_is_rejected_but_jwks_query_is_supported():
    with pytest.raises(ValueError):
        JWTAuthConfig(issuer=ISSUER + "?tenant=1", audience=AUDIENCE)
    assert JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, jwks_uri=JWKS_URI + "?v=1")


@pytest.mark.parametrize(
    "changes",
    [
        {"audience": ""},
        {"audience": []},
        {"audience": " spaced "},
        {"algorithms": ()},
        {"algorithms": ("HS256",)},
        {"algorithms": ("none",)},
        {"algorithms": ("RS256", "RS256")},
        {"algorithms": "RS256"},
        {"required_claims": ()},
        {"required_claims": ("",)},
        {"required_claims": "sub"},
        {"leeway_seconds": -1},
        {"leeway_seconds": 301},
        {"leeway_seconds": float("nan")},
        {"cache_ttl_seconds": 0},
        {"cache_ttl_seconds": 3601},
        {"refresh_interval_seconds": 0},
        {"refresh_interval_seconds": 301},
        {"cache_ttl_seconds": 5, "refresh_interval_seconds": 10},
        {"timeout_seconds": True},
        {"timeout_seconds": 31},
        {"timeout_seconds": float("inf")},
    ],
)
def test_invalid_authentication_policy_is_rejected_at_configuration(changes):
    with pytest.raises(ValueError):
        JWTAuthConfig(**{"issuer": ISSUER, "audience": AUDIENCE, **changes})


@pytest.mark.parametrize(
    "change",
    [
        {"kid": ""},
        {"kty": "oct"},
        {"alg": "HS256"},
        {"alg": "RS512"},
        {"use": "enc"},
        {"key_ops": ["sign"]},
        {"key_ops": "verify"},
        {"n": "invalid"},
        {"n": None},
        {"n": "A" * 4097},
        {"d": "private"},
    ],
)
def test_unusable_or_malformed_signing_keys_fail_closed(signing_key, change):
    provider = _Provider(signing_key)
    provider.keys = [{**_jwk(signing_key), **change}]
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(signing_key))


def test_duplicate_key_ids_are_rejected(signing_key, rotated_key):
    provider = _Provider(signing_key)
    provider.keys = [_jwk(signing_key), _jwk(rotated_key)]
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(signing_key))


@pytest.mark.parametrize(
    "body", [b"not json", b"[]", b'{"keys":[]}', b'{"keys":null}', b'{"keys":[],"keys":[]}']
)
def test_malformed_key_documents_are_rejected(signing_key, body):
    provider = _Provider(signing_key, jwks_uri=JWKS_URI)
    provider.response = httpx.Response(200, content=body)
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(signing_key))


def test_response_key_count_and_token_sizes_are_bounded(signing_key):
    provider = _Provider(signing_key, jwks_uri=JWKS_URI)
    provider.keys = [_jwk(signing_key, kid=str(index)) for index in range(101)]
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(signing_key))
    provider.now += 30
    provider.response = httpx.Response(200, content=b" " * 1_048_577)
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(signing_key))
    before = len(provider.requests)
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate("x" * 16_385)
    assert len(provider.requests) == before


def test_slow_trickling_response_obeys_total_deadline(signing_key):
    provider = _Provider(signing_key, jwks_uri=JWKS_URI)

    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(20_000):
                provider.now += 1
                yield b" "

    provider.response = httpx.Response(200, stream=Trickle())
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(signing_key))
    assert provider.now == 1006


def test_compressed_responses_cannot_bypass_size_and_deadline_limits(signing_key):
    provider = _Provider(signing_key, jwks_uri=JWKS_URI)
    provider.response = httpx.Response(
        200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"compressed")
    )
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(signing_key))


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", "é", "e30.W10.", "e30.bnVsbA."])
def test_malformed_tokens_are_rejected_without_network_requests(signing_key, token):
    provider = _Provider(signing_key)
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(token)
    assert provider.requests == []


@pytest.mark.parametrize("segment", [0, 1, 2], ids=["header", "payload", "signature"])
@pytest.mark.parametrize("suffix", ["!!!!", "=="], ids=["ignored-punctuation", "padding"])
def test_noncanonical_compact_segments_are_rejected_before_fetch(signing_key, segment, suffix):
    provider = _Provider(signing_key)
    parts = _token(signing_key).split(".")
    parts[segment] += suffix
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(".".join(parts))
    assert provider.requests == []


@pytest.mark.parametrize("mutation", ["standard-alphabet", "nonzero-pad-bits"])
def test_noncanonical_signature_encoding_is_rejected_before_fetch(signing_key, mutation):
    provider = _Provider(signing_key)
    parts = _token(signing_key).split(".")
    if mutation == "standard-alphabet":
        parts[2] = "+" + parts[2][1:]
    else:
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        original = jwt.utils.base64url_decode(parts[2])
        # A 2048-bit RSA signature leaves four unused bits in its final symbol.
        parts[2] = parts[2][:-1] + alphabet[alphabet.index(parts[2][-1]) + 1]
        assert jwt.utils.base64url_decode(parts[2]) == original
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(".".join(parts))
    assert provider.requests == []


def test_duplicate_jwt_claims_are_rejected_before_fetch(signing_key):
    provider = _Provider(signing_key)
    header = jwt.utils.base64url_encode(b'{"alg":"RS256","kid":"first"}')
    payload = jwt.utils.base64url_encode(b'{"sub":"alice","sub":"bob"}')
    token = b".".join([header, payload, b"signature"]).decode()
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(token)
    assert provider.requests == []


@pytest.mark.parametrize(
    "algorithm",
    ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"],
)
def test_all_supported_asymmetric_algorithms_can_validate(signing_key, algorithm):
    if algorithm.startswith("ES"):
        curve = {"ES256": ec.SECP256R1, "ES384": ec.SECP384R1, "ES512": ec.SECP521R1}[algorithm]
        key = ec.generate_private_key(curve())
    elif algorithm == "EdDSA":
        key = ed25519.Ed25519PrivateKey.generate()
    else:
        key = signing_key
    provider = _Provider(signing_key, algorithms=(algorithm,))
    provider.keys = [_jwk(key, algorithm=algorithm)]
    assert provider.auth.authenticate(_token(key, algorithm=algorithm)).subject == "alice"


def test_jwk_algorithm_cannot_override_verifiers_algorithm_policy(signing_key):
    provider = _Provider(signing_key, algorithms=("RS512",))
    entry = _jwk(signing_key)
    entry.pop("alg")
    provider.keys = [entry]
    header = jwt.utils.base64url_encode(b'{"alg":"RS512","kid":"first"}')
    payload = jwt.utils.base64url_encode(json.dumps(_claims()).encode())
    signing_input = b".".join([header, payload])
    signature = jwt.algorithms.get_default_algorithms()["RS256"].sign(signing_input, signing_key)
    forged = b".".join([signing_input, jwt.utils.base64url_encode(signature)]).decode()
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(forged)
    assert provider.auth.authenticate(_token(signing_key, algorithm="RS512")).subject == "alice"


def test_weak_rsa_and_mismatched_elliptic_curve_are_rejected(signing_key):
    provider = _Provider(signing_key)
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    provider.keys = [_jwk(weak)]
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(signing_key))
    ec_key = ec.generate_private_key(ec.SECP384R1())
    provider = _Provider(signing_key, algorithms=("ES256",))
    provider.keys = [_jwk(ec_key, algorithm="ES384") | {"alg": "ES256"}]
    permitted_curve = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(permitted_curve, algorithm="ES256"))


def test_close_clears_keys_and_keeps_injected_client_owned_by_caller(signing_key):
    provider = _Provider(signing_key)
    provider.auth.authenticate(_token(signing_key))
    provider.auth.close()
    assert not provider.client.is_closed
    assert provider.auth._keys == {}
    with pytest.raises(AuthenticationError):
        provider.auth.authenticate(_token(signing_key))
    provider.auth.close()
    owned = JWTAuthenticator(replace(provider.auth.config, jwks_uri=JWKS_URI))
    owned.close()
    assert owned._client.is_closed
