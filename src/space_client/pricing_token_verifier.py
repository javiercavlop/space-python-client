"""Verifying pricing tokens with the public keys SPACE publishes (JWKS).

SPACE versions with the capability ``pricing-token-jwks`` sign pricing tokens
with ES256 and publish the public keys at ``GET /api/v1/.well-known/jwks.json``.
:class:`PricingTokenVerifier` (and :class:`AsyncPricingTokenVerifier`) fetch
that set, cache it for the ``max-age`` of the response, fetch it again when a
token names an unknown ``kid`` (a key rotation, at most once per
``refetch_cooldown``), and check:

* the signature: ES256 only, by the published key the ``kid`` names; keys are
  never taken from ``jwk``, ``jku``, ``x5u`` or ``x5c`` headers;
* ``exp`` (required; ``leeway`` adds tolerance);
* ``iss``: ``space`` unless another ``issuer`` is configured;
* ``aud`` and ``organizationId``: the expected organization (required).

The JOSE work is done by PyJWT with ``cryptography`` (extra ``verify``:
``pip install 'space-python-client[verify]'``), imported on first use, so
applications that never verify tokens do not need it. The JWKS itself is
fetched with httpx, like every other call of the client.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any

import httpx

from .errors import (
    PricingTokenAudienceError,
    PricingTokenExpiredError,
    PricingTokenIssuerError,
    PricingTokenSignatureError,
    PricingTokenUnknownKeyError,
    PricingTokenVerificationError,
    SpaceFeatureUnsupportedError,
)
from .types.pricing_token import PricingTokenPayload

PRICING_TOKEN_JWKS = "pricing-token-jwks"
"""The ``X-Space-Capabilities`` token of SPACE versions that publish the keys."""

DEFAULT_ISSUER = "space"
"""The ``iss`` of pricing tokens when SPACE has no ``JWT_ISSUER`` configured."""

DEFAULT_CACHE_MAX_AGE = 300.0
DEFAULT_REFETCH_COOLDOWN = 30.0

# Headers that point at or carry a key. SPACE never sends them.
_FORBIDDEN_HEADERS = ("jwk", "jku", "x5u", "x5c")


def _jwt() -> Any:
    try:
        # Optional dependency (extra "verify"), loaded on first use.
        import jwt
        from jwt.algorithms import has_crypto
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise ImportError(
            "Verifying pricing tokens needs PyJWT with cryptography: "
            "pip install 'space-python-client[verify]'"
        ) from error
    if not has_crypto:  # pragma: no cover - depends on the environment
        raise ImportError(
            "Verifying pricing tokens needs the cryptography package: "
            "pip install 'space-python-client[verify]'"
        )
    return jwt


@dataclass
class _KeySet:
    keys: dict[str, Any]
    expires_at: float


def cache_max_age(header: str | None) -> float:
    """Seconds a JWKS response may be cached: its ``max-age``, 0 for ``no-store``/``no-cache``, else 300."""
    if not header or not header.strip():
        return DEFAULT_CACHE_MAX_AGE
    directives = [d.strip() for d in header.lower().split(",")]
    if "no-store" in directives or "no-cache" in directives:
        return 0.0
    for directive in directives:
        if directive.startswith("max-age="):
            try:
                seconds = float(directive[len("max-age=") :])
            except ValueError:
                break
            if seconds >= 0:
                return seconds
    return DEFAULT_CACHE_MAX_AGE


class _VerifierBase:
    def __init__(
        self,
        url: str | None = None,
        *,
        jwks_url: str | None = None,
        organization_id: str | None = None,
        issuer: str = DEFAULT_ISSUER,
        leeway: float = 0,
        refetch_cooldown: float = DEFAULT_REFETCH_COOLDOWN,
        timeout: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if jwks_url:
            self._jwks_url = jwks_url
        elif url:
            self._jwks_url = f"{url.rstrip('/')}/api/v1/.well-known/jwks.json"
        else:
            raise ValueError("A pricing token verifier needs the url of SPACE (or jwks_url)")
        self._organization_id = organization_id
        self._issuer = issuer
        self._leeway = leeway
        self._refetch_cooldown = refetch_cooldown
        self._timeout = timeout
        self._clock = clock
        self._key_set: _KeySet | None = None
        self._last_fetch = float("-inf")

    def clear_cache(self) -> None:
        """Forget the cached JWKS, so that the next verification fetches it again."""
        self._key_set = None

    # -- steps shared by both verifiers ------------------------------------

    def _organization(self, organization_id: str | None) -> str:
        organization = organization_id or self._organization_id
        if not organization:
            raise ValueError("Verifying a pricing token needs the organization_id it must be issued for")
        return organization

    def _cached(self) -> _KeySet | None:
        if self._key_set is not None and self._clock() < self._key_set.expires_at:
            return self._key_set
        return None

    def _may_refetch(self) -> bool:
        return self._clock() - self._last_fetch >= self._refetch_cooldown

    def _key_set_from(self, response: httpx.Response) -> _KeySet:
        # Every SPACE response carries X-Space-Capabilities (S0). Without the
        # token the server is older or in legacy HS256 mode; without the header
        # it predates S0, unless it is a 5xx that may come from a proxy.
        raw = response.headers.get("x-space-capabilities")
        if raw is None:
            unsupported = response.status_code < 500
        else:
            unsupported = PRICING_TOKEN_JWKS not in {t.strip().lower() for t in raw.split(",")}
        if unsupported:
            raise SpaceFeatureUnsupportedError(
                PRICING_TOKEN_JWKS,
                "The connected Space does not publish the keys of its pricing tokens "
                "(pricing-token-jwks): it is older, or signs them with the legacy HS256",
            )
        try:
            body = response.json() if response.status_code == 200 else None
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("keys"), list):
            raise PricingTokenVerificationError(
                "PRICING_TOKEN_JWKS_UNAVAILABLE",
                f"Space answered {response.status_code} to {self._jwks_url}",
            )
        jwt = _jwt()
        keys: dict[str, Any] = {}
        for jwk in body["keys"]:
            if not _is_es256_signing_key(jwk):
                continue
            try:
                keys[jwk["kid"]] = jwt.PyJWK(jwk, algorithm="ES256")
            except jwt.PyJWTError:
                continue
        self._key_set = _KeySet(keys, self._clock() + cache_max_age(response.headers.get("cache-control")))
        return self._key_set

    def _unavailable(self, error: Exception) -> PricingTokenVerificationError:
        failure = PricingTokenVerificationError(
            "PRICING_TOKEN_JWKS_UNAVAILABLE", f"Could not fetch the pricing token keys from {self._jwks_url}"
        )
        failure.__cause__ = error
        return failure

    def _decode(
        self, token: str, key: Any, organization: str, issuer: str | None, leeway: float | None
    ) -> PricingTokenPayload:
        jwt = _jwt()
        expected_issuer = issuer or self._issuer
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["ES256"],
                audience=organization,
                issuer=expected_issuer,
                leeway=self._leeway if leeway is None else leeway,
                options={"require": ["exp", "sub", "iss", "aud"]},
            )
        except jwt.InvalidSignatureError as error:
            raise PricingTokenSignatureError() from error
        except jwt.ExpiredSignatureError as error:
            raise PricingTokenExpiredError(_expired_at(token)) from error
        except jwt.InvalidAudienceError as error:
            raise PricingTokenAudienceError(organization, _unverified(token).get("aud")) from error
        except jwt.InvalidIssuerError as error:
            raise PricingTokenIssuerError(expected_issuer, _unverified(token).get("iss")) from error
        except jwt.MissingRequiredClaimError as error:
            if error.claim == "aud":
                raise PricingTokenAudienceError(organization, None) from error
            if error.claim == "iss":
                raise PricingTokenIssuerError(expected_issuer, None) from error
            raise PricingTokenVerificationError("PRICING_TOKEN_INVALID", f"The pricing token is invalid: {error}") from error
        except jwt.PyJWTError as error:
            raise PricingTokenVerificationError("PRICING_TOKEN_INVALID", f"The pricing token is invalid: {error}") from error

        if claims.get("organizationId") != organization:
            raise PricingTokenAudienceError(organization, claims.get("organizationId"))
        return PricingTokenPayload.from_dict(claims)


class PricingTokenVerifier(_VerifierBase):
    """Verifies pricing tokens with the JWKS of a SPACE (synchronous).

    Keep one per SPACE and reuse it, so that the cached keys are shared. It can
    be used from several threads.

    Args:
        url: The URL of SPACE, as given to the client.
        jwks_url: The JWKS URL itself, instead of deriving it from ``url``.
        organization_id: The organization tokens must be issued for; required
            here or on every ``verify`` call.
        issuer: The expected ``iss`` (``JWT_ISSUER`` of your SPACE). Default ``space``.
        leeway: Tolerance for ``exp``/``nbf``/``iat``, in seconds. Default 0.
        refetch_cooldown: Minimum seconds between two fetches triggered by an
            unknown ``kid``. Default 30.
        timeout: Timeout of the JWKS request, in seconds. Default 5.
        http_client: An ``httpx.Client`` to use (not closed by ``close``).
        clock: Monotonic clock of the cache, in seconds (tests).
    """

    def __init__(self, url: str | None = None, *, http_client: httpx.Client | None = None, **options: Any) -> None:
        super().__init__(url, **options)
        self._own_client = http_client is None
        self._http = http_client or httpx.Client()
        self._lock = threading.Lock()

    def verify(
        self,
        token: str,
        organization_id: str | None = None,
        issuer: str | None = None,
        leeway: float | None = None,
    ) -> PricingTokenPayload:
        """Verify ``token`` and return its payload.

        Raises:
            ValueError: Without an organization_id (here or in the constructor).
            PricingTokenVerificationError: Or one of its subclasses
                (signature, expired, audience, issuer, unknown kid).
            SpaceFeatureUnsupportedError: When SPACE does not publish its keys
                (capability ``pricing-token-jwks``).
        """
        organization = self._organization(organization_id)
        kid = _check_header(token)
        key_set = self._key_set_for(kid)
        if kid not in key_set.keys:
            raise PricingTokenUnknownKeyError(kid)
        return self._decode(token, key_set.keys[kid], organization, issuer, leeway)

    def close(self) -> None:
        if self._own_client:
            self._http.close()

    def __enter__(self) -> "PricingTokenVerifier":
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.close()

    def _key_set_for(self, kid: str) -> _KeySet:
        with self._lock:
            key_set = self._cached()
            if key_set is not None and (kid in key_set.keys or not self._may_refetch()):
                return key_set
            return self._fetch()

    def _fetch(self) -> _KeySet:
        self._last_fetch = self._clock()
        try:
            response = self._http.get(
                self._jwks_url,
                timeout=self._timeout,
                headers={"Accept": "application/jwk-set+json, application/json"},
            )
        except httpx.HTTPError as error:
            raise self._unavailable(error) from error
        return self._key_set_from(response)


class AsyncPricingTokenVerifier(_VerifierBase):
    """The asynchronous :class:`PricingTokenVerifier`: same arguments, ``verify`` is awaited.

    ``http_client`` is an ``httpx.AsyncClient``. Concurrent verifications share
    one JWKS request.
    """

    def __init__(
        self, url: str | None = None, *, http_client: httpx.AsyncClient | None = None, **options: Any
    ) -> None:
        super().__init__(url, **options)
        self._own_client = http_client is None
        self._http = http_client or httpx.AsyncClient()
        self._lock: asyncio.Lock | None = None

    async def verify(
        self,
        token: str,
        organization_id: str | None = None,
        issuer: str | None = None,
        leeway: float | None = None,
    ) -> PricingTokenPayload:
        """Verify ``token`` and return its payload (see :meth:`PricingTokenVerifier.verify`)."""
        organization = self._organization(organization_id)
        kid = _check_header(token)
        key_set = await self._key_set_for(kid)
        if kid not in key_set.keys:
            raise PricingTokenUnknownKeyError(kid)
        return self._decode(token, key_set.keys[kid], organization, issuer, leeway)

    async def close(self) -> None:
        if self._own_client:
            await self._http.aclose()

    async def __aenter__(self) -> "AsyncPricingTokenVerifier":
        return self

    async def __aexit__(self, *_exc_info: Any) -> None:
        await self.close()

    async def _key_set_for(self, kid: str) -> _KeySet:
        # Created lazily: an asyncio.Lock binds to the running loop on Python 3.10.
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            key_set = self._cached()
            if key_set is not None and (kid in key_set.keys or not self._may_refetch()):
                return key_set
            return await self._fetch()

    async def _fetch(self) -> _KeySet:
        self._last_fetch = self._clock()
        try:
            response = await self._http.get(
                self._jwks_url,
                timeout=self._timeout,
                headers={"Accept": "application/jwk-set+json, application/json"},
            )
        except httpx.HTTPError as error:
            raise self._unavailable(error) from error
        return self._key_set_from(response)


def _check_header(token: str) -> str:
    """Check the protected header before any key is looked up; return its ``kid``."""
    jwt = _jwt()
    if not isinstance(token, str) or token.count(".") != 2:
        raise PricingTokenVerificationError("PRICING_TOKEN_INVALID", "The pricing token is malformed")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as error:
        raise PricingTokenVerificationError("PRICING_TOKEN_INVALID", "The pricing token is malformed") from error
    if header.get("alg") != "ES256":
        raise PricingTokenVerificationError(
            "PRICING_TOKEN_INVALID", f"Pricing tokens must be signed with ES256, not {header.get('alg')!r}"
        )
    for name in _FORBIDDEN_HEADERS:
        if name in header:
            raise PricingTokenVerificationError(
                "PRICING_TOKEN_INVALID",
                f'The pricing token has a "{name}" header; keys are only taken from the JWKS of Space',
            )
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise PricingTokenVerificationError("PRICING_TOKEN_INVALID", "The pricing token names no key (kid)")
    return kid


def _is_es256_signing_key(jwk: Any) -> bool:
    return (
        isinstance(jwk, dict)
        and jwk.get("kty") == "EC"
        and jwk.get("crv") == "P-256"
        and isinstance(jwk.get("kid"), str)
        and jwk.get("alg") in (None, "ES256")
        and jwk.get("use") in (None, "sig")
        and "d" not in jwk
    )


def _unverified(token: str) -> dict[str, Any]:
    """Claims for error details only, read after the signature was checked."""
    try:
        claims = _jwt().decode(token, options={"verify_signature": False})
    except Exception:  # noqa: BLE001 - details only
        return {}
    return claims if isinstance(claims, dict) else {}


def _expired_at(token: str) -> datetime | None:
    exp = _unverified(token).get("exp")
    if isinstance(exp, (int, float)):
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    return None


__all__ = [
    "DEFAULT_ISSUER",
    "PRICING_TOKEN_JWKS",
    "AsyncPricingTokenVerifier",
    "PricingTokenVerifier",
    "cache_max_age",
]
