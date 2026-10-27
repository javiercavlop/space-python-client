"""Event ids, deduplication by ``eventId`` and resync after a reconnection.

Space servers with the ``contract-event-ids`` capability add ``eventId`` (the
same in every copy of one event) and ``occurredAt`` to every event. Delivery is
at most once per connection, without replay or ordering.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from space_client import AsyncSpaceClient, EventIdWindow, SpaceClient
from space_client.types import (
    CacheOptions,
    EventDeduplicationOptions,
    EventDetails,
    ResyncInfo,
    SpaceConnectionOptions,
    SpaceEvent,
)

DETAILS = {
    "userId": "u1",
    "organizationId": "org-1",
    "contractVersion": "5:x",
    "services": ["acme"],
}


def event(event_id: str | None = "019a2a6b-7c1e-7d3f-9b2a-5c4e8f1a2b3c", **extra: Any) -> dict:
    message: dict[str, Any] = {"code": "CONTRACT_NOVATED", "details": dict(DETAILS)}
    if event_id is not None:
        message["eventId"] = event_id
        message["occurredAt"] = "2026-10-27T10:00:00.123Z"
    message.update(extra)
    return message


def options(**kwargs) -> SpaceConnectionOptions:
    return SpaceConnectionOptions(url="http://space.test", api_key="org-key", **kwargs)


def novations(client: SpaceClient) -> list[Any]:
    received: list[Any] = []
    client.on("contract_novated", received.append)
    return received


# -- event details -------------------------------------------------------------


def test_listeners_get_the_details_with_event_id_and_occurred_at():
    client = SpaceClient(options())
    received = novations(client)

    client._socket.trigger("message", event(), namespace="/contracts")

    (details,) = received
    assert isinstance(details, EventDetails)
    assert details == DETAILS  # still the plain details dict
    assert details.code == "CONTRACT_NOVATED"
    assert details.event_id == "019a2a6b-7c1e-7d3f-9b2a-5c4e8f1a2b3c"
    assert details.raw_occurred_at == "2026-10-27T10:00:00.123Z"
    assert details.occurred_at == datetime(2026, 10, 27, 10, 0, 0, 123000, tzinfo=timezone.utc)
    client.close()


def test_occurred_at_is_none_when_missing_or_not_a_date():
    client = SpaceClient(options())
    received = novations(client)

    client._socket.trigger("message", event(None), namespace="/contracts")
    client._socket.trigger(
        "message", event("e-2", occurredAt="not a date"), namespace="/contracts"
    )

    assert received[0].event_id is None
    assert received[0].occurred_at is None
    assert received[0].raw_occurred_at is None
    assert received[1].occurred_at is None
    assert received[1].raw_occurred_at == "not a date"
    client.close()


def test_pricing_listeners_get_event_details_too():
    client = SpaceClient(options())
    received: list[Any] = []
    client.on("pricing_created", received.append)
    message = {
        "code": "PRICING_CREATED",
        "eventId": "p-1",
        "occurredAt": "2026-10-27T10:00:00.000Z",
        "details": {"serviceName": "acme", "pricingVersion": "2025"},
    }

    client._socket.trigger("message", message)
    client._socket.trigger("message", message)

    (details,) = received
    assert details == {"serviceName": "acme", "pricingVersion": "2025"}
    assert details.event_id == "p-1"
    client.close()


# -- deduplication ---------------------------------------------------------------


def test_a_repeated_event_id_is_delivered_once():
    client = SpaceClient(options())
    received = novations(client)

    client._socket.trigger("message", event("a"), namespace="/contracts")
    client._socket.trigger("message", event("a"), namespace="/contracts")
    client._socket.trigger("message", event("b"), namespace="/contracts")

    assert [d.event_id for d in received] == ["a", "b"]
    client.close()


def test_a_repeat_does_not_invalidate_the_cache_again():
    client = SpaceClient(options(cache=CacheOptions(enabled=True)))
    received = novations(client)
    key = client.cache.get_contract_key("u1")

    client._socket.trigger("message", event("a"), namespace="/contracts")
    client.cache.set(key, {"fresh": True})  # re-read after the first copy
    client._socket.trigger("message", event("a"), namespace="/contracts")

    assert len(received) == 1
    assert client.cache.get(key) == {"fresh": True}
    client.close()


def test_events_without_event_id_are_passed_through():
    client = SpaceClient(options())
    received = novations(client)

    client._socket.trigger("message", event(None), namespace="/contracts")
    client._socket.trigger("message", event(None), namespace="/contracts")

    assert len(received) == 2
    client.close()


def test_deduplication_can_be_turned_off():
    for off in (False, EventDeduplicationOptions(enabled=False)):
        client = SpaceClient(options(event_deduplication=off))
        received = novations(client)

        client._socket.trigger("message", event("a"), namespace="/contracts")
        client._socket.trigger("message", event("a"), namespace="/contracts")

        assert len(received) == 2
        client.close()


def test_the_window_forgets_the_oldest_ids_beyond_max_entries():
    client = SpaceClient(options(event_deduplication=EventDeduplicationOptions(max_entries=2)))
    received = novations(client)

    for event_id in ["a", "b", "c", "a", "c"]:
        client._socket.trigger("message", event(event_id), namespace="/contracts")

    # 'a' was evicted by 'c' (window of two: b, c); 'c' is still known.
    assert [d.event_id for d in received] == ["a", "b", "c", "a"]
    client.close()


def test_the_async_client_drops_repeats_too():
    client = AsyncSpaceClient(options())
    received: list[Any] = []
    client.on("contract_novated", received.append)

    async def scenario():
        async with client as space:
            await space._socket.trigger_async("message", event("a"), namespace="/contracts")
            await space._socket.trigger_async("message", event("a"), namespace="/contracts")

    asyncio.run(scenario())
    assert [d.event_id for d in received] == ["a"]


def test_event_id_window_accepts_an_id_once():
    window = EventIdWindow()
    assert window.accept("x") is True
    assert window.accept("x") is False
    assert window.accept("y") is True
    assert len(window) == 2


def test_event_id_window_forgets_ids_older_than_the_window():
    now = [0.0]
    window = EventIdWindow(window_seconds=1.0, clock=lambda: now[0])
    assert window.accept("x") is True
    now[0] = 0.999
    assert window.accept("x") is False
    now[0] = 1.001
    assert window.accept("x") is True


def test_event_id_window_keeps_at_most_max_entries():
    window = EventIdWindow(max_entries=3)
    for event_id in ["1", "2", "3", "4"]:
        assert window.accept(event_id) is True
    assert len(window) == 3
    assert window.accept("1") is True  # evicted
    assert window.accept("4") is False


def test_event_id_window_defaults():
    assert EventIdWindow.DEFAULT_MAX_ENTRIES == 10_000
    assert EventIdWindow.DEFAULT_WINDOW_SECONDS == 600
    defaults = EventDeduplicationOptions()
    assert (defaults.enabled, defaults.max_entries, defaults.window_seconds) == (True, 10_000, 600)


@pytest.mark.parametrize(
    "kwargs",
    [{"max_entries": 0}, {"max_entries": 1.5}, {"window_seconds": 0}, {"window_seconds": -1}],
)
def test_event_id_window_rejects_invalid_options(kwargs):
    with pytest.raises(ValueError):
        EventIdWindow(**kwargs)


# -- resync ------------------------------------------------------------------------


def test_resync_is_a_known_event_name():
    assert SpaceEvent.RESYNC.value == "resync"


def test_resync_fires_on_first_connection_and_on_reconnection():
    client = SpaceClient(options())
    resyncs: list[ResyncInfo] = []
    client.on("resync", resyncs.append)
    novations(client)

    client._socket.trigger("connect", namespace="/pricings")
    client._socket.trigger("connect", namespace="/contracts")
    client._socket.trigger("connect", namespace="/contracts")
    client._socket.trigger("connect", namespace="/pricings")

    assert resyncs == [
        ResyncInfo(namespace="pricings", reconnect=False),
        ResyncInfo(namespace="contracts", reconnect=False),
        ResyncInfo(namespace="contracts", reconnect=True),
        ResyncInfo(namespace="pricings", reconnect=True),
    ]
    client.close()


def test_a_contracts_reconnection_invalidates_cached_users_before_resync():
    client = SpaceClient(options(cache=CacheOptions(enabled=True)))
    novations(client)
    cache = client.cache
    seen: list[Any] = []
    client.on(
        "resync",
        lambda info: seen.append((info, cache.get(cache.get_contract_key("u7")))),
    )
    client._socket.trigger("connect", namespace="/contracts")
    cache.set(cache.get_contract_key("u7"), {"stale": True})
    cache.set(cache.get_pricing_token_key("u8"), "stale-token")
    cache.set(cache.get_feature_key("u8", "acme-storage"), {"eval": True})

    client._socket.trigger("connect", namespace="/contracts")

    assert seen[-1] == (ResyncInfo(namespace="contracts", reconnect=True), None)
    assert cache.get(cache.get_pricing_token_key("u8")) is None
    assert cache.get(cache.get_feature_key("u8", "acme-storage")) is None
    client.close()


def test_the_synchronized_listener_still_fires():
    client = SpaceClient(options())
    synchronized: list[Any] = []
    client.on("synchronized", synchronized.append)
    client._socket.trigger("connect", namespace="/pricings")
    assert synchronized == [None]
    client.close()
