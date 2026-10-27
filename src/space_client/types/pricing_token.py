"""The payload of a pricing token, for code that already holds one.

``features.generate_user_pricing_token`` returns a JWT signed by Space. A host
backend that hands it to a browser sometimes needs to read it too: which
versions, plan and add-ons the user is on, and the ``contractVersion`` to send
with a change. :func:`decode_pricing_token` reads the payload WITHOUT verifying
the signature - it is for reading a token you just received from Space, not
for authenticating one somebody else gave you.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PricingTokenSubscription:
    """Claim ``subscription``: the contract the token was issued for."""

    contracted_services: dict[str, str] = field(default_factory=dict)
    subscription_plans: dict[str, str] = field(default_factory=dict)
    subscription_add_ons: dict[str, dict[str, int]] = field(default_factory=dict)
    contract_version: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PricingTokenSubscription | None":
        if data is None:
            return None
        return cls(
            contracted_services=dict(data.get("contractedServices") or {}),
            subscription_plans=dict(data.get("subscriptionPlans") or {}),
            subscription_add_ons=dict(data.get("subscriptionAddOns") or {}),
            contract_version=data.get("contractVersion"),
        )


@dataclass
class PricingTokenPayload:
    """Decoded pricing token.

    ``sub`` is opaque: do not parse it and do not use it as the userId. Space
    versions with per-organization userIds (capability
    ``contract-userid-per-org``) send
    ``urn:space:<organizationId>:<percent-encoded userId>``; older versions send
    the bare userId. Use :func:`pricing_token_user_id` to get the userId.

    ``user_id`` (claim ``userId``, the userId as stored in Space),
    ``organization_id`` (``organizationId``), ``aud`` (the organization the
    token is for, equal to ``organizationId``) and ``iss`` (the Space server,
    ``space`` unless it sets ``JWT_ISSUER``) are None for tokens of older
    versions. ``subscription`` is None for tokens issued by Space versions that
    do not include it. ``raw`` keeps every claim as sent.
    """

    sub: str | None = None
    iat: int | None = None
    exp: int | None = None
    features: dict[str, Any] = field(default_factory=dict)
    pricing_context: dict[str, Any] | None = None
    subscription_context: dict[str, Any] | None = None
    subscription: PricingTokenSubscription | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    # Added after the existing fields so that positional construction keeps its meaning.
    user_id: str | None = None
    organization_id: str | None = None
    aud: str | list[str] | None = None
    iss: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PricingTokenPayload":
        return cls(
            sub=data.get("sub"),
            user_id=data.get("userId"),
            organization_id=data.get("organizationId"),
            aud=data.get("aud"),
            iss=data.get("iss"),
            iat=data.get("iat"),
            exp=data.get("exp"),
            features=dict(data.get("features") or {}),
            pricing_context=data.get("pricingContext"),
            subscription_context=data.get("subscriptionContext"),
            subscription=PricingTokenSubscription.from_dict(data.get("subscription")),
            raw=data,
        )


def decode_pricing_token(token: str) -> PricingTokenPayload:
    """Read the payload of a pricing token without verifying its signature.

    Args:
        token (str): A JWT as returned by ``generate_user_pricing_token``.

    Returns:
        PricingTokenPayload: The decoded claims.

    Raises:
        ValueError: If the token is not a JWT with a JSON object payload.
    """
    parts = token.split(".") if token else []
    if len(parts) != 3:
        raise ValueError("A pricing token is a JWT with three dot-separated parts")
    segment = parts[1]
    try:
        decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        payload = json.loads(decoded)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError(f"The pricing token payload is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("The pricing token payload is not a JSON object")
    return PricingTokenPayload.from_dict(payload)


def pricing_token_user_id(payload: PricingTokenPayload) -> str:
    """The userId a pricing token was issued for.

    Reads the ``userId`` claim. Only tokens without it (issued by Space versions
    that predate per-organization userIds, where ``sub`` is the bare userId)
    fall back to ``sub``. ``sub`` is never parsed: on newer servers it is an
    opaque ``urn:space:...`` subject. This does not verify the token.

    Raises:
        TypeError: If the token has a ``userId`` claim that is not a string.
        ValueError: If the token has neither ``userId`` nor ``sub``.
    """
    user_id = payload.user_id
    if user_id is not None:
        if not isinstance(user_id, str):
            raise TypeError("The userId claim of the pricing token is not a string")
        return user_id
    if payload.sub is None:
        raise ValueError("The pricing token has neither a userId nor a sub claim")
    return payload.sub


__all__ = [
    "PricingTokenPayload",
    "PricingTokenSubscription",
    "decode_pricing_token",
    "pricing_token_user_id",
]
