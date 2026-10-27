from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from space_client import AsyncSpaceClient, SpaceApiError, SpaceClientFactory, SpaceConnectionError
from space_client.modules.contract_module import ContractModule, _ContractCalls
from space_client.modules.feature_module import FeatureModule, _FeatureCalls
from space_client.types import CacheOptions, SpaceConnectionOptions
from space_client.types.models import Subscription

# The tests drive coroutines with `asyncio.run` rather than a plugin, so the
# package keeps the dev dependencies it already had.


class FakeResponse:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self):
        return str(self._payload)


def a_client(**kwargs) -> AsyncSpaceClient:
    return AsyncSpaceClient(
        SpaceConnectionOptions(url="http://localhost:3000", api_key="api-key", **kwargs)
    )


def responds(client: AsyncSpaceClient, handler, monkeypatch, record: list | None = None):
    async def fake_request(method, url, headers=None, json=None):
        if record is not None:
            record.append((method, url, json))
        return handler(method, url, json)

    monkeypatch.setattr(client._http_client, "request", fake_request)


def test_evaluate_revert_and_pricing_token(monkeypatch):
    client = a_client(cache=CacheOptions(enabled=True))
    calls: list = []

    def handler(method, url, json):
        if method == "POST" and "/features/u1/f1?revert=true" in url:
            return FakeResponse(200, {"ok": True})
        if method == "POST" and "/features/u1/f1" in url:
            return FakeResponse(200, {"eval": True, "used": {"calls": 1}, "limit": {"calls": 10}, "error": None})
        if method == "POST" and url.endswith("/features/u1/pricing-token"):
            return FakeResponse(200, {"pricingToken": "token-123"})
        return FakeResponse(500, {"error": "unexpected"})

    responds(client, handler, monkeypatch, calls)

    async def scenario():
        result = await client.features.evaluate("u1", "f1")
        assert result.eval is True

        # A read-only evaluation is served from cache the second time, so the
        # caching that makes the sync client cheap is not lost by going async.
        before = len(calls)
        second = await client.features.evaluate("u1", "f1")
        assert second.eval is True
        assert len(calls) == before

        with pytest.deprecated_call():
            assert await client.features.revert_evaluation("u1", "f1") is True
        assert await client.features.generate_user_pricing_token("u1") == "token-123"

        await client.close()

    asyncio.run(scenario())


def test_contract_round_trip(monkeypatch):
    client = a_client(cache=CacheOptions(enabled=True))

    contract = {
        "userContact": {"userId": "u1", "username": "u1"},
        "billingPeriod": {"autoRenew": True, "renewalDays": 30},
        "usageLevels": {},
        "contractedServices": {},
        "subscriptionPlans": {"petclinic": "BASIC"},
        "subscriptionAddOns": {},
        "history": [],
    }

    def handler(method, url, json):
        if method in {"GET", "PUT"} and "/contracts/u1" in url:
            return FakeResponse(200, contract)
        if method == "DELETE" and "/contracts/u1" in url:
            return FakeResponse(204, None)
        return FakeResponse(500, {"error": "unexpected"})

    responds(client, handler, monkeypatch)

    async def scenario():
        fetched = await client.contracts.get_contract("u1")
        assert fetched is not None and fetched.user_id == "u1"

        updated = await client.contracts.update_contract_subscription(
            "u1", Subscription(contracted_services={}, subscription_plans={"petclinic": "PRO"})
        )
        assert updated is not None

        assert await client.contracts.remove_contract("u1") is None
        await client.close()

    asyncio.run(scenario())


def test_a_refusal_raises_rather_than_returning_none(monkeypatch):
    client = a_client()
    responds(client, lambda *_: FakeResponse(403, {"error": "nope"}), monkeypatch)

    async def scenario():
        with pytest.raises(SpaceApiError) as caught:
            await client.contracts.get_contract("u1")

        assert caught.value.status_code == 403
        await client.close()

    asyncio.run(scenario())


def test_an_outage_raises_a_connection_error(monkeypatch):
    client = a_client()

    async def explode(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(client._http_client, "request", explode)

    async def scenario():
        with pytest.raises(SpaceConnectionError):
            await client.contracts.get_contract("u1")
        await client.close()

    asyncio.run(scenario())


def test_healthcheck(monkeypatch):
    client = a_client()
    responds(client, lambda *_: FakeResponse(200, {"message": "ok"}), monkeypatch)

    async def scenario():
        assert await client.is_connected_to_space() is True
        await client.close()

    asyncio.run(scenario())


def test_used_as_an_async_context_manager(monkeypatch):
    client = a_client()
    responds(client, lambda *_: FakeResponse(200, {"message": "ok"}), monkeypatch)

    async def scenario():
        async with client as space:
            # The constructor cannot await, so the socket is opened here.
            assert space._socket.connected is True
            assert await space.is_connected_to_space() is True

    asyncio.run(scenario())


def test_the_factory_builds_one_and_validates_the_same_way():
    client = SpaceClientFactory.connect_async("http://localhost:3000", "api-key")
    assert isinstance(client, AsyncSpaceClient)

    # A constructor that does not await also does not connect.
    assert client._socket.connected is False

    with pytest.raises(ValueError):
        SpaceClientFactory.connect_async("localhost:3000", "api-key")
    with pytest.raises(ValueError):
        SpaceClientFactory.connect_async("http://localhost:3000", "")
    with pytest.raises(ValueError):
        SpaceClientFactory.connect_async("http://localhost:3000", "api-key", timeout=0)


def test_the_two_clients_offer_the_same_calls():
    """The point of sharing the bodies: the surfaces cannot drift apart.

    If someone adds a method to one module and forgets the other, this fails -
    which is the failure the shared `_*Calls` classes exist to make impossible.
    """
    for shared, sync_module in ((_ContractCalls, ContractModule), (_FeatureCalls, FeatureModule)):
        declared = {name for name in vars(shared) if not name.startswith("_")}
        assert declared, "the shared class should declare the calls"
        for name in declared:
            assert hasattr(sync_module, name)


def test_the_async_calls_are_awaitable_and_the_sync_ones_are_not(monkeypatch):
    client = a_client()
    responds(client, lambda *_: FakeResponse(200, {"message": "ok"}), monkeypatch)

    async def scenario():
        pending = client.features.evaluate("u1", "f1")
        assert inspect.isawaitable(pending), "an async module method must be awaitable"
        await pending
        await client.close()

    asyncio.run(scenario())
