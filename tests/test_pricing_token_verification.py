"""Verifying pricing tokens with the JWKS SPACE publishes (space#87).

The keys are generated here, and the JWKS is served by ``respx`` at the URL a
real SPACE uses. Every test runs with the synchronous and the asynchronous
verifier (``flavour``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
import jwt
import pytest
import respx
from conftest import API_URL, SPACE_URL
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from space_client import (
    AsyncPricingTokenVerifier,
    AsyncSpaceClient,
    PricingTokenAudienceError,
    PricingTokenExpiredError,
    PricingTokenIssuerError,
    PricingTokenSignatureError,
    PricingTokenUnknownKeyError,
    PricingTokenVerificationError,
    PricingTokenVerifier,
    SpaceClient,
    SpaceFeatureUnsupportedError,
)
from space_client.types import PricingTokenPayload, SpaceConnectionOptions, pricing_token_user_id

ORG = "6630a1b2c3d4e5f6a7b8c9d0"
OTHER_ORG = "6630a1b2c3d4e5f6a7b8c9ff"
JWKS_URL = f"{API_URL}/.well-known/jwks.json"
CAPABILITIES = "contract-userid-per-org,pricing-token-jwks"


@dataclass
class Key:
    private: ec.EllipticCurvePrivateKey
    public_jwk: dict[str, Any]
    kid: str


def new_key() -> Key:
    private = ec.generate_private_key(ec.SECP256R1())
    jwk = ECAlgorithm.to_jwk(private.public_key(), as_dict=True)
    members = {name: jwk[name] for name in ("crv", "kty", "x", "y")}
    canonical = json.dumps(members, separators=(",", ":"), sort_keys=True).encode()
    kid = base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).rstrip(b"=").decode()
    return Key(private, {**members, "kid": kid, "alg": "ES256", "use": "sig"}, kid)


def sign(
    key: Key,
    user_id: str = "user-1",
    organization_id: str = ORG,
    audience: str | None = None,
    issuer: str = "space",
    exp: int | None = None,
    omit_exp: bool = False,
    headers: dict[str, Any] | None = None,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": f"urn:space:{organization_id}:{quote(user_id, safe='')}",
        "userId": user_id,
        "organizationId": organization_id,
        "aud": audience or organization_id,
        "iss": issuer,
        "iat": now,
        "features": {},
    }
    if not omit_exp:
        claims["exp"] = exp if exp is not None else now + 3600
    return jwt.encode(claims, key.private, algorithm="ES256", headers={"kid": key.kid, **(headers or {})})


def b64(value: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def claims_of(token: str) -> dict[str, Any]:
    segment = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Jwks:
    """What the mocked SPACE publishes."""

    def __init__(self, key: Key) -> None:
        self.keys = [key.public_jwk]
        self.cache_control: str | None = "public, max-age=300"
        self.capabilities: str | None = CAPABILITIES
        self.status = 200

    def response(self, _request: httpx.Request) -> httpx.Response:
        headers = {"Content-Type": "application/jwk-set+json"}
        if self.cache_control is not None:
            headers["Cache-Control"] = self.cache_control
        if self.capabilities is not None:
            headers["X-Space-Capabilities"] = self.capabilities
        body = {"keys": self.keys} if self.status == 200 else {"error": "not available"}
        return httpx.Response(self.status, json=body, headers=headers)


@pytest.fixture
def key() -> Key:
    return new_key()


@pytest.fixture
def jwks(key: Key):
    published = Jwks(key)
    with respx.mock(assert_all_called=False) as router:
        published.router = router
        published.route = router.get(JWKS_URL).mock(side_effect=published.response)
        yield published


@pytest.fixture
def make(flavour: str):
    """``make(**options)`` -> ``verify(token, **kwargs)`` for the requested flavour."""
    loop = asyncio.new_event_loop() if flavour == "async" else None
    made: list[Any] = []

    def factory(**options: Any):
        options.setdefault("url", SPACE_URL)
        options.setdefault("organization_id", ORG)
        verifier: Any = (AsyncPricingTokenVerifier if loop else PricingTokenVerifier)(**options)
        made.append(verifier)

        def verify(token: str, **kwargs: Any) -> PricingTokenPayload:
            result = verifier.verify(token, **kwargs)
            return loop.run_until_complete(result) if loop else result

        verify.verifier = verifier  # type: ignore[attr-defined]
        return verify

    yield factory
    for verifier in made:
        closing = verifier.close()
        if loop:
            loop.run_until_complete(closing)
    if loop:
        loop.close()


def calls(jwks: Jwks) -> int:
    return jwks.route.call_count


def test_a_valid_token_is_accepted_and_typed(jwks, key, make):
    payload = make()(sign(key, user_id="a:b/ñ"))

    assert isinstance(payload, PricingTokenPayload)
    assert payload.user_id == "a:b/ñ" and pricing_token_user_id(payload) == "a:b/ñ"
    assert payload.organization_id == ORG and payload.aud == ORG and payload.iss == "space"
    assert payload.sub == f"urn:space:{ORG}:{quote('a:b/ñ', safe='')}"
    assert calls(jwks) == 1
    assert "x-api-key" not in jwks.route.calls.last.request.headers


def test_the_organization_is_required(jwks, key, make):
    verify = make(organization_id=None)
    with pytest.raises(ValueError, match="organization_id"):
        verify(sign(key))
    assert verify(sign(key), organization_id=ORG).organization_id == ORG


def test_another_key_under_a_published_kid_is_rejected(jwks, key, make):
    attacker = new_key()
    forged = sign(Key(attacker.private, attacker.public_jwk, key.kid))

    with pytest.raises(PricingTokenSignatureError) as error:
        make()(forged)
    assert error.value.code == "PRICING_TOKEN_SIGNATURE_INVALID"


def test_a_tampered_payload_is_rejected(jwks, key, make):
    header, _, signature = sign(key).split(".")
    forged = f"{header}.{b64({**claims_of(sign(key)), 'userId': 'admin'})}.{signature}"
    with pytest.raises(PricingTokenSignatureError):
        make()(forged)


def test_an_unknown_kid_is_rejected(jwks, key, make):
    other = new_key()
    # A JWKS fetched just now is not fetched again.
    with pytest.raises(PricingTokenUnknownKeyError) as error:
        make()(sign(other))
    assert error.value.kid == other.kid and error.value.code == "PRICING_TOKEN_UNKNOWN_KID"
    assert calls(jwks) == 1

    # A cached JWKS older than the cooldown is fetched once more first.
    clock = Clock()
    verify = make(clock=clock)
    verify(sign(key))
    clock.now += 60
    with pytest.raises(PricingTokenUnknownKeyError):
        verify(sign(other))
    assert calls(jwks) == 3


def test_an_expired_token_is_rejected_with_an_optional_leeway(jwks, key, make):
    now = int(time.time())
    token = sign(key, exp=now - 10)

    with pytest.raises(PricingTokenExpiredError) as error:
        make()(token)
    assert error.value.code == "PRICING_TOKEN_EXPIRED"
    assert error.value.expired_at is not None and int(error.value.expired_at.timestamp()) == now - 10

    assert make(leeway=60)(token).user_id == "user-1"
    assert make()(token, leeway=60).user_id == "user-1"
    with pytest.raises(PricingTokenExpiredError):
        make(leeway=60)(sign(key, exp=now - 120))


def test_a_token_without_exp_is_rejected(jwks, key, make):
    with pytest.raises(PricingTokenVerificationError) as error:
        make()(sign(key, omit_exp=True))
    assert error.value.code == "PRICING_TOKEN_INVALID"


def test_a_token_of_another_organization_is_rejected(jwks, key, make):
    with pytest.raises(PricingTokenAudienceError) as error:
        make()(sign(key, organization_id=OTHER_ORG))
    assert error.value.code == "PRICING_TOKEN_WRONG_AUDIENCE" and error.value.expected == ORG

    # aud of this organization, organizationId of another.
    with pytest.raises(PricingTokenAudienceError):
        make()(sign(key, organization_id=OTHER_ORG, audience=ORG))
    # The organization of the call wins over the one of the verifier.
    with pytest.raises(PricingTokenAudienceError):
        make()(sign(key), organization_id=OTHER_ORG)


def test_the_issuer_is_space_by_default_or_the_configured_one(jwks, key, make):
    token = sign(key, issuer="https://space.example.com")
    with pytest.raises(PricingTokenIssuerError) as error:
        make()(token)
    assert error.value.code == "PRICING_TOKEN_WRONG_ISSUER" and error.value.expected == "space"

    assert make(issuer="https://space.example.com")(token).iss == "https://space.example.com"
    assert make()(token, issuer="https://space.example.com").iss == "https://space.example.com"
    with pytest.raises(PricingTokenIssuerError):
        make(issuer="https://space.example.com")(sign(key))


def test_an_hs256_token_is_rejected_even_with_a_published_kid(jwks, key, make):
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u", "userId": "u", "organizationId": ORG, "aud": ORG, "iss": "space", "iat": now, "exp": now + 60},
        "a-shared-secret-of-thirty-two-bytes!",
        algorithm="HS256",
        headers={"kid": key.kid},
    )
    with pytest.raises(PricingTokenVerificationError, match="HS256") as error:
        make()(token)
    assert error.value.code == "PRICING_TOKEN_INVALID"


@pytest.mark.parametrize("header", [{"alg": "none"}, {"alg": "none", "kid": "k"}, {"alg": "NONE", "kid": "k"}])
def test_an_unsecured_token_is_rejected(jwks, key, make, header):
    now = int(time.time())
    body = {"sub": "u", "userId": "u", "organizationId": ORG, "aud": ORG, "iss": "space", "iat": now, "exp": now + 60}
    with pytest.raises(PricingTokenVerificationError) as error:
        make()(f"{b64({**header, 'kid': key.kid} if 'kid' in header else header)}.{b64(body)}.")
    assert error.value.code == "PRICING_TOKEN_INVALID"


def test_jku_x5u_and_embedded_jwk_headers_are_never_followed(jwks, key, make):
    attacker = new_key()
    attacker_url = f"{SPACE_URL}/attacker/jwks.json"
    attacker_route = jwks.router.get(attacker_url).mock(return_value=httpx.Response(200, json={"keys": [attacker.public_jwk]}))
    tokens = [
        sign(attacker, headers={"jku": attacker_url}),
        sign(attacker, headers={"x5u": attacker_url}),
        sign(attacker, headers={"jwk": {k: attacker.public_jwk[k] for k in ("kty", "crv", "x", "y")}}),
        # Even the right key and kid: SPACE never sends these headers.
        sign(key, headers={"jku": attacker_url}),
    ]
    for token in tokens:
        with pytest.raises(PricingTokenVerificationError) as error:
            make()(token)
        assert error.value.code == "PRICING_TOKEN_INVALID"
    assert attacker_route.call_count == 0


@pytest.mark.parametrize("token", ["", "abc", "a.b.c", f"{b64({'alg': 'ES256'})}.{b64({})}."])
def test_malformed_tokens_are_rejected(jwks, make, token):
    with pytest.raises(PricingTokenVerificationError) as error:
        make()(token)
    assert error.value.code == "PRICING_TOKEN_INVALID"


def test_a_space_without_pricing_token_jwks_is_reported(jwks, key, make):
    token = sign(key)
    # SPACE before space#87: no route.
    jwks.status, jwks.capabilities = 404, "contract-userid-per-org"
    with pytest.raises(SpaceFeatureUnsupportedError) as error:
        make()(token)
    assert error.value.capability == "pricing-token-jwks" and error.value.written is False

    # Legacy HS256 mode: the route answers, the capability is not advertised.
    jwks.status = 200
    with pytest.raises(SpaceFeatureUnsupportedError):
        make()(token)

    # A SPACE that predates X-Space-Capabilities.
    jwks.capabilities = None
    with pytest.raises(SpaceFeatureUnsupportedError):
        make()(token)


def test_an_unavailable_jwks_is_a_verification_error(jwks, key, make):
    jwks.status = 503
    with pytest.raises(PricingTokenVerificationError) as error:
        make()(sign(key))
    assert error.value.code == "PRICING_TOKEN_JWKS_UNAVAILABLE"

    jwks.route.side_effect = httpx.ConnectError("refused")
    with pytest.raises(PricingTokenVerificationError) as error:
        make()(sign(key))
    assert error.value.code == "PRICING_TOKEN_JWKS_UNAVAILABLE"


def test_the_jwks_is_cached_for_its_max_age(jwks, key, make):
    jwks.cache_control = "public, max-age=10"
    clock = Clock()
    verify = make(clock=clock)
    token = sign(key)

    verify(token)
    verify(token)
    clock.now += 9
    verify(token)
    assert calls(jwks) == 1
    clock.now += 2
    verify(token)
    assert calls(jwks) == 2


def test_five_minutes_without_cache_control_and_no_cache_with_no_store(jwks, key, make):
    jwks.cache_control = None
    clock = Clock()
    verify = make(clock=clock)
    token = sign(key)
    verify(token)
    clock.now += 299
    verify(token)
    assert calls(jwks) == 1
    clock.now += 2
    verify(token)
    assert calls(jwks) == 2

    jwks.cache_control = "no-store"
    no_store = make(clock=clock)
    no_store(token)
    no_store(token)
    assert calls(jwks) == 4


def test_an_unknown_kid_refetches_the_jwks_with_a_cooldown(jwks, key, make):
    clock = Clock()
    verify = make(clock=clock, refetch_cooldown=30)
    verify(sign(key))
    assert calls(jwks) == 1

    # Rotation: the new key signs, the old one stays published.
    rotated = new_key()
    jwks.keys = [rotated.public_jwk, key.public_jwk]
    clock.now += 31
    assert verify(sign(rotated)).user_id == "user-1"
    assert calls(jwks) == 2
    verify(sign(key))
    assert calls(jwks) == 2

    unknown = new_key()
    clock.now += 1
    for _ in range(2):
        with pytest.raises(PricingTokenUnknownKeyError):
            verify(sign(unknown))
    assert calls(jwks) == 2

    clock.now += 30
    with pytest.raises(PricingTokenUnknownKeyError):
        verify(sign(unknown))
    assert calls(jwks) == 3


def test_a_removed_key_stops_verifying_once_the_cache_expires(jwks, key, make):
    clock = Clock()
    verify = make(clock=clock)
    token = sign(key)
    verify(token)
    jwks.keys = [new_key().public_jwk]
    clock.now += 301
    with pytest.raises(PricingTokenUnknownKeyError):
        verify(token)


def test_published_keys_that_are_not_es256_signing_keys_are_ignored(jwks, key, make):
    jwks.keys = [{**key.public_jwk, "use": "enc"}, {**key.public_jwk, "crv": "P-384"}]
    with pytest.raises(PricingTokenUnknownKeyError):
        make()(sign(key))


def test_concurrent_async_verifications_fetch_the_jwks_once(jwks, key):
    async def scenario() -> None:
        verifier = AsyncPricingTokenVerifier(url=SPACE_URL, organization_id=ORG)
        token = sign(key)
        await asyncio.gather(*(verifier.verify(token) for _ in range(5)))
        await verifier.close()

    asyncio.run(scenario())
    assert calls(jwks) == 1


@pytest.mark.parametrize("client_class", [SpaceClient, AsyncSpaceClient])
def test_features_verify_pricing_token_uses_the_organization_of_the_client(jwks, key, client_class):
    with_org = client_class(SpaceConnectionOptions(url=SPACE_URL, api_key="usr_key", organization_id=ORG))
    org_key = client_class(SpaceConnectionOptions(url=SPACE_URL, api_key="org-key"))
    token = sign(key)
    try:
        if client_class is SpaceClient:
            assert with_org.features.verify_pricing_token(token).user_id == "user-1"
            assert org_key.features.verify_pricing_token(token, organization_id=ORG).user_id == "user-1"
            with pytest.raises(ValueError):
                org_key.features.verify_pricing_token(token)
            with pytest.raises(PricingTokenAudienceError):
                with_org.features.verify_pricing_token(token, organization_id=OTHER_ORG)
        else:

            async def scenario() -> None:
                assert (await with_org.features.verify_pricing_token(token)).user_id == "user-1"
                payload = await org_key.features.verify_pricing_token(token, organization_id=ORG)
                assert payload.user_id == "user-1"
                with pytest.raises(ValueError):
                    await org_key.features.verify_pricing_token(token)
                await with_org.close()
                await org_key.close()

            asyncio.run(scenario())
    finally:
        if client_class is SpaceClient:
            with_org.close()
            org_key.close()
    assert "x-api-key" not in jwks.route.calls.last.request.headers
