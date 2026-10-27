"""The claims SPACE adds to pricing tokens with per-organization userIds (space#80).

``sub`` became an opaque ``urn:space:<organizationId>:<percent-encoded userId>``
and the token gained ``userId``, ``organizationId``, ``aud`` and ``iss``. Tokens
of older servers only have the bare userId in ``sub``.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import quote

import pytest

from space_client.types import PricingTokenPayload, decode_pricing_token, pricing_token_user_id

ORG = "6630a1b2c3d4e5f6a7b8c9d0"


def a_token(payload: dict[str, Any]) -> str:
    def part(data: dict[str, Any]) -> str:
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{part({'alg': 'ES256', 'kid': 'k1'})}.{part(payload)}.signature"


def new_server_claims(user_id: str) -> dict[str, Any]:
    return {
        "sub": f"urn:space:{ORG}:{quote(user_id, safe='')}",
        "userId": user_id,
        "organizationId": ORG,
        "aud": ORG,
        "iss": "space",
        "iat": 1761559200,
        "exp": 1761562800,
        "features": {},
    }


def old_server_claims(user_id: str) -> dict[str, Any]:
    return {"sub": user_id, "iat": 1761559200, "exp": 1761562800, "features": {}}


def test_decode_reads_the_new_claims():
    payload = decode_pricing_token(a_token(new_server_claims("user-1")))

    assert payload.user_id == "user-1"
    assert payload.organization_id == ORG
    assert payload.aud == ORG
    assert payload.iss == "space"
    assert payload.sub == f"urn:space:{ORG}:user-1"
    assert pricing_token_user_id(payload) == "user-1"


@pytest.mark.parametrize("user_id", ["a:b", "a%3Ab", "with space", "mail@example.com", "a/b", "ñandú"])
def test_the_user_id_is_the_raw_claim_not_part_of_the_urn(user_id):
    payload = decode_pricing_token(a_token(new_server_claims(user_id)))

    assert payload.sub != user_id
    assert pricing_token_user_id(payload) == user_id


def test_sub_is_never_parsed_when_the_token_has_a_user_id():
    claims = {**new_server_claims("user-1"), "sub": "urn:space:other-org:someone-else"}
    assert pricing_token_user_id(decode_pricing_token(a_token(claims))) == "user-1"


def test_tokens_of_older_servers_fall_back_to_sub():
    payload = decode_pricing_token(a_token(old_server_claims("user-1")))

    assert payload.user_id is None
    assert payload.organization_id is None and payload.aud is None and payload.iss is None
    assert pricing_token_user_id(payload) == "user-1"
    # An old token whose userId looks like a URN is returned as it is.
    old_urn_like = decode_pricing_token(a_token(old_server_claims("urn:space:x:y")))
    assert pricing_token_user_id(old_urn_like) == "urn:space:x:y"


def test_aud_may_be_a_list():
    claims = {**new_server_claims("user-1"), "aud": [ORG, "other"]}
    assert decode_pricing_token(a_token(claims)).aud == [ORG, "other"]


def test_a_user_id_claim_that_is_not_a_string_is_an_error_not_a_fallback():
    claims = {**new_server_claims("user-1"), "userId": 42}
    with pytest.raises(TypeError, match="userId claim"):
        pricing_token_user_id(decode_pricing_token(a_token(claims)))


def test_payload_built_by_hand_defaults_to_no_new_claims():
    payload = PricingTokenPayload(sub="user-1")
    assert (payload.user_id, payload.organization_id, payload.aud, payload.iss) == (None, None, None, None)
    assert pricing_token_user_id(payload) == "user-1"


def test_a_token_without_sub_or_user_id_has_no_user():
    with pytest.raises(ValueError, match="neither"):
        pricing_token_user_id(PricingTokenPayload())
