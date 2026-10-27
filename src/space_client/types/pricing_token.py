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

    ``subscription`` is None for tokens issued by Space versions that do not
    include it. ``raw`` keeps every claim as sent.
    """

    sub: str | None = None
    iat: int | None = None
    exp: int | None = None
    features: dict[str, Any] = field(default_factory=dict)
    pricing_context: dict[str, Any] | None = None
    subscription_context: dict[str, Any] | None = None
    subscription: PricingTokenSubscription | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PricingTokenPayload":
        return cls(
            sub=data.get("sub"),
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


__all__ = ["PricingTokenPayload", "PricingTokenSubscription", "decode_pricing_token"]
