"""Contract events (S6) and the subscription carried by pricing tokens (S7)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import pytest

from space_client import AsyncSpaceClient, SpaceClient
from space_client.types import (
    CacheOptions,
    PricingTokenPayload,
    PricingTokenSubscription,
    SpaceConnectionOptions,
    SpaceEvent,
    decode_pricing_token,
)

CONTRACT_EVENT = {
    "code": "CONTRACT_NOVATED",
    "details": {
        "userId": "u1",
        "organizationId": "org-1",
        "contractVersion": "5:2026-10-27T10:06:00.000Z",
        "services": ["acme"],
    },
}


def options(**kwargs) -> SpaceConnectionOptions:
    return SpaceConnectionOptions(url="http://space.test", api_key="org-key", **kwargs)


# -- S6: events --------------------------------------------------------------


def test_the_contract_events_are_known_event_names():
    assert {e.value for e in SpaceEvent} >= {
        "contract_created",
        "contract_novated",
        "contract_deleted",
        "contract_renewed",
    }


def test_without_contract_listeners_only_the_pricings_namespace_is_opened():
    client = SpaceClient(options())
    (first,) = client._socket.connect_calls
    assert first["namespaces"] == ["/pricings"]
    client.close()


def test_registering_a_contract_listener_opens_the_authenticated_contracts_namespace():
    client = SpaceClient(options())
    received: list[Any] = []

    client.on("contract_novated", received.append)

    last = client._socket.connect_calls[-1]
    assert last["namespaces"] == ["/pricings", "/contracts"]
    assert last["auth"] == {"apiKey": "org-key"}
    assert last["headers"] == {"x-api-key": "org-key"}

    client._socket.trigger("message", CONTRACT_EVENT, namespace="/contracts")
    assert received == [CONTRACT_EVENT["details"]]

    # A second contract listener does not reconnect again.
    calls = len(client._socket.connect_calls)
    client.on("contract_created", received.append)
    assert len(client._socket.connect_calls) == calls
    client.close()


def test_a_member_user_key_sends_its_organization_with_the_handshake():
    client = SpaceClient(options(organization_id="org-1"))
    assert client._socket.connect_calls[-1].get("auth") is None  # /pricings stays anonymous

    client.on("contract_novated", lambda _d: None)

    last = client._socket.connect_calls[-1]
    assert last["auth"] == {"apiKey": "org-key", "organizationId": "org-1"}
    assert last["headers"] == {"x-api-key": "org-key"}
    client.close()


def test_an_org_key_without_organization_sends_only_the_key():
    client = SpaceClient(options())
    client.on("contract_novated", lambda _d: None)
    assert client._socket.connect_calls[-1]["auth"] == {"apiKey": "org-key"}
    client.close()


def test_a_deletion_carries_a_null_contract_version():
    client = SpaceClient(options(cache=CacheOptions(enabled=True)))
    client.cache.set(client.cache.get_contract_key("u1"), {"x": 1})
    deleted: list[Any] = []
    client.on("contract_deleted", deleted.append)
    details = {**CONTRACT_EVENT["details"], "contractVersion": None}

    client._socket.trigger("message", {"code": "CONTRACT_DELETED", "details": details}, namespace="/contracts")

    assert deleted == [details] and deleted[0]["contractVersion"] is None
    assert client.cache.get(client.cache.get_contract_key("u1")) is None
    client.close()


def test_contract_events_reach_only_their_own_listener():
    client = SpaceClient(options())
    novated: list[Any] = []
    deleted: list[Any] = []
    client.on("contract_novated", novated.append)
    client.on("CONTRACT_DELETED", deleted.append)

    client._socket.trigger("message", {**CONTRACT_EVENT, "code": "CONTRACT_DELETED"}, namespace="/contracts")
    # A pricing-namespace message with a contract code is not a contract event.
    client._socket.trigger("message", CONTRACT_EVENT, namespace="/pricings")

    assert novated == []
    assert deleted == [CONTRACT_EVENT["details"]]
    client.close()


def test_a_contract_event_invalidates_the_users_cache_before_the_callback():
    client = SpaceClient(options(cache=CacheOptions(enabled=True)))
    key = client.cache.get_contract_key("u1")
    client.cache.set(key, {"userContact": {"userId": "u1"}})
    seen_in_callback: list[Any] = []
    client.on("contract_novated", lambda _details: seen_in_callback.append(client.cache.get(key)))

    client._socket.trigger("message", CONTRACT_EVENT, namespace="/contracts")

    assert seen_in_callback == [None]
    client.close()


def test_a_rejected_key_on_the_contracts_namespace_reports_an_error():
    client = SpaceClient(options())
    errors: list[Any] = []
    client.on("error", errors.append)
    client.on("contract_created", lambda _d: None)

    client._socket.trigger("connect_error", {"message": "Invalid API key"}, namespace="/contracts")

    assert errors == [{"message": "Invalid API key"}]
    client.close()


def test_async_client_opens_the_contracts_namespace_when_it_connects():
    client = AsyncSpaceClient(options(cache=CacheOptions(enabled=True)))
    received: list[Any] = []
    client.cache.set(client.cache.get_contract_key("u1"), {"x": 1})

    async def scenario():
        client.on("contract_renewed", received.append)
        async with client as space:
            last = space._socket.connect_calls[-1]
            assert last["namespaces"] == ["/pricings", "/contracts"]
            assert last["auth"] == {"apiKey": "org-key"}
            await space._socket.trigger_async(
                "message", {**CONTRACT_EVENT, "code": "CONTRACT_RENEWED"}, namespace="/contracts"
            )

    asyncio.run(scenario())
    assert received == [CONTRACT_EVENT["details"]]


def test_async_client_reconnects_on_connect_when_contract_listeners_were_added_later():
    client = AsyncSpaceClient(options())

    async def scenario():
        await client.connect()
        assert client._socket.connect_calls[-1]["namespaces"] == ["/pricings"]
        client.on("contract_created", lambda _d: None)
        await client.connect()
        assert client._socket.connect_calls[-1]["namespaces"] == ["/pricings", "/contracts"]
        await client.close()

    asyncio.run(scenario())


def test_async_on_after_connect_warns_that_connect_must_be_called_again(caplog):
    client = AsyncSpaceClient(options())

    async def scenario():
        client.on("contract_created", lambda _d: None)  # before connecting: no warning
        assert not caplog.records
        await client.connect()
        with caplog.at_level(logging.WARNING, logger="space_client"):
            client.on("contract_novated", lambda _d: None)  # already in /contracts: fine
            assert not caplog.records
        await client.disconnect()
        client.remove_all_listeners()
        await client.connect()
        with caplog.at_level(logging.WARNING, logger="space_client"):
            client.on("contract_novated", lambda _d: None)
            client.on("pricing_created", lambda _d: None)  # not a contract event
        await client.close()

    asyncio.run(scenario())
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "await client.connect()" in warnings[0].getMessage()


# -- S7: the subscription in a pricing token ---------------------------------


def a_token(payload: dict[str, Any]) -> str:
    def part(data: dict[str, Any]) -> str:
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{part({'alg': 'HS256'})}.{part(payload)}.signature"


TOKEN_PAYLOAD = {
    "sub": "u1",
    "iat": 1761559200,
    "exp": 1761562800,
    "features": {"acme-storage": {"eval": True, "used": {"acme-storage": 4}, "limit": {"acme-storage": 10}}},
    "pricingContext": {"features": {}, "usageLimits": {}},
    "subscriptionContext": {"acme-storage": 4},
    "subscription": {
        "contractedServices": {"acme": "2025-01-10"},
        "subscriptionPlans": {"acme": "growth"},
        "subscriptionAddOns": {"acme": {"extraStorage": 2}},
        "contractVersion": "5:2026-10-27T10:06:00.000Z",
    },
}


def test_decode_pricing_token_reads_the_subscription():
    payload = decode_pricing_token(a_token(TOKEN_PAYLOAD))

    assert isinstance(payload, PricingTokenPayload)
    assert payload.sub == "u1" and payload.exp == 1761562800
    assert payload.features["acme-storage"]["eval"] is True
    subscription = payload.subscription
    assert isinstance(subscription, PricingTokenSubscription)
    assert subscription.contracted_services == {"acme": "2025-01-10"}
    assert subscription.subscription_plans == {"acme": "growth"}
    assert subscription.subscription_add_ons == {"acme": {"extraStorage": 2}}
    assert subscription.contract_version == "5:2026-10-27T10:06:00.000Z"
    assert payload.raw == TOKEN_PAYLOAD


def test_tokens_from_older_servers_have_no_subscription():
    legacy = {key: value for key, value in TOKEN_PAYLOAD.items() if key != "subscription"}
    assert decode_pricing_token(a_token(legacy)).subscription is None


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.!!!.c"])
def test_decode_pricing_token_rejects_malformed_tokens(token):
    with pytest.raises(ValueError):
        decode_pricing_token(token)
