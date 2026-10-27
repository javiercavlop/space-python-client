from __future__ import annotations

from typing import Any

import pytest

from space_client.space_client import SpaceClient
from space_client.types import CacheOptions, SpaceConnectionOptions


class FakeResponse:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_evaluate_revert_and_pricing_token(monkeypatch):
    options = SpaceConnectionOptions(
        url="http://localhost:3000",
        api_key="api-key",
        cache=CacheOptions(enabled=True),
    )
    client = SpaceClient(options)

    def fake_request(method, url, headers=None, json=None):
        # The revert goes to the feature route (it used to go to /features/u1,
        # which evaluates every feature and reverts nothing).
        if method == "POST" and "/api/v1/features/u1/f1" in url and "pricing-token" not in url and "revert=true" not in url:
            return FakeResponse(200, {"eval": True, "used": {"calls": 1}, "limit": {"calls": 10}, "error": None})

        if method == "POST" and "/api/v1/features/u1/f1?revert=true" in url:
            return FakeResponse(200, {"ok": True})

        if method == "POST" and url.endswith("/api/v1/features/u1/pricing-token"):
            return FakeResponse(200, {"pricingToken": "token-123"})

        return FakeResponse(500, {"error": "unexpected"})

    monkeypatch.setattr(client._http_client, "request", fake_request)

    result = client.features.evaluate("u1", "f1")
    assert result.eval is True

    # Cached read-only evaluation should return same result without transport changes.
    second = client.features.evaluate("u1", "f1")
    assert second.eval is True

    with pytest.deprecated_call():
        assert client.features.revert_evaluation("u1", "f1") is True

    token = client.features.generate_user_pricing_token("u1")
    assert token == "token-123"

    # Cached token
    token2 = client.features.generate_user_pricing_token("u1")
    assert token2 == "token-123"
