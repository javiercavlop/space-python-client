"""Idempotency-Key, retries and revert by consumption id (SCORELabUS/space#84, #85).

* Every write SPACE can apply at most once carries an ``Idempotency-Key``: the
  caller's or a new UUID v4, the same on each of the client's own retries.
* Retries are opt-in (``retries``), only for those writes, only after a network
  error, a ``5xx`` or ``409 IDEMPOTENCY_IN_PROGRESS``, only while Space
  advertises ``idempotency-key``, and they wait ``Retry-After``.
* ``consumption_id`` of an evaluation, and ``revert_consumption`` by that id.

Every test runs against both clients.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import warnings
from typing import Any

import httpx
import pytest
import respx
from conftest import API_URL, SPACE_URL

from space_client import (
    AsyncSpaceClient,
    IdempotencyConflictError,
    IdempotencyError,
    IdempotencyInProgressError,
    IdempotencyKeyInvalidError,
    SpaceApiError,
    SpaceClient,
    SpaceConnectionError,
    SpaceFeatureUnsupportedError,
)
from space_client.types import (
    BillingPeriodToCreate,
    ContractToCreate,
    SpaceConnectionOptions,
    Subscription,
    UserContact,
)

UUID_V4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
CAPABILITIES = {"X-Space-Capabilities": "atomic-usage-limits,idempotency-key,contract-version"}
OLD_SPACE = {"X-Space-Capabilities": "contract-version"}

FEATURE_URL = f"{API_URL}/features/u1/acme-pets"
EVALUATION = {
    "eval": True,
    "used": {"acme-maxPets": 2},
    "limit": {"acme-maxPets": 4},
    "error": None,
    "consumptionId": "6f1c2a5e-6a7b-4c4d-9e8f-0a1b2c3d4e5f",
}
CONTRACT = {
    "userContact": {"userId": "u1", "username": "u1"},
    "billingPeriod": {"startDate": "2026-10-01", "endDate": "2026-11-01", "autoRenew": True, "renewalDays": 30},
    "usageLevels": {},
    "contractedServices": {"acme": "2025-01-10"},
    "subscriptionPlans": {"acme": "pro"},
    "subscriptionAddOns": {},
    "history": [],
}
SUBSCRIPTION = Subscription(
    contracted_services={"acme": "2025-01-10"},
    subscription_plans={"acme": "basic"},
    subscription_add_ons={},
)


def in_progress(retry_after: str = "1") -> httpx.Response:
    return httpx.Response(
        409,
        json={"error": "A request with this Idempotency-Key is still being processed", "code": "IDEMPOTENCY_IN_PROGRESS"},
        headers={**CAPABILITIES, "Retry-After": retry_after},
    )


def ok(body: Any = None, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(status, json=EVALUATION if body is None else body, headers={**CAPABILITIES, **headers})


def healthcheck(headers: dict | None = None):
    return respx.get(f"{API_URL}/healthcheck").mock(
        return_value=httpx.Response(200, json={"message": "ok"}, headers=CAPABILITIES if headers is None else headers)
    )


def keys(route) -> list[str | None]:
    return [call.request.headers.get("idempotency-key") for call in route.calls]


@pytest.fixture
def make(flavour: str, monkeypatch):
    """Builds a client of the flavour with the given options; its waits are recorded, not slept."""
    loop = asyncio.new_event_loop() if flavour == "async" else None
    clients: list[Any] = []
    sleeps: list[float] = []

    def build(**options: Any):
        opts = SpaceConnectionOptions(url=SPACE_URL, api_key="org-key", **options)
        client: Any = AsyncSpaceClient(opts) if flavour == "async" else SpaceClient(opts)
        clients.append(client)
        if hasattr(client, "_sleep"):
            if flavour == "async":

                async def record(seconds: float) -> None:
                    sleeps.append(seconds)

            else:

                def record(seconds: float) -> None:  # type: ignore[misc]
                    sleeps.append(seconds)

            monkeypatch.setattr(client, "_sleep", record)
        return client

    def call(result: Any) -> Any:
        if inspect.isawaitable(result):
            assert loop is not None
            return loop.run_until_complete(result)
        assert loop is None
        return result

    yield build, call, sleeps

    for client in clients:
        call(client.close())
    if loop is not None:
        loop.close()


# -- the key ------------------------------------------------------------------


@respx.mock
def test_consumption_sends_a_uuid_v4_key_and_returns_the_consumption_id(make):
    build, call, _ = make
    route = respx.post(FEATURE_URL).mock(return_value=ok())
    client = build()

    result = call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}))

    assert result.consumption_id == EVALUATION["consumptionId"]
    assert UUID_V4.match(keys(route)[0] or "")


@respx.mock
def test_every_call_gets_a_new_key(make):
    build, call, _ = make
    route = respx.post(FEATURE_URL).mock(return_value=ok())
    client = build()

    call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}))
    call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}))

    first, second = keys(route)
    assert first != second


@respx.mock
def test_an_evaluation_that_does_not_consume_sends_no_key(make):
    build, call, _ = make
    route = respx.post(FEATURE_URL).mock(return_value=ok({"eval": True, "used": {}, "limit": {}, "error": None}))
    client = build()

    result = call(client.features.evaluate("u1", "acme-pets"))

    assert keys(route) == [None]
    assert result.consumption_id is None


@respx.mock
def test_the_callers_key_is_sent_verbatim(make):
    build, call, _ = make
    healthcheck()
    route = respx.post(FEATURE_URL).mock(return_value=ok())
    client = build()

    call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}, idempotency_key="order-42:pets"))

    assert keys(route) == ["order-42:pets"]


@respx.mock
@pytest.mark.parametrize("key", ["", "k" * 256])
def test_an_invalid_key_is_rejected_before_anything_is_sent(make, key):
    build, call, _ = make
    route = respx.post(FEATURE_URL).mock(return_value=ok())
    client = build()

    with pytest.raises(ValueError):
        call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}, idempotency_key=key))
    assert route.call_count == 0


@respx.mock
def test_a_callers_key_is_refused_when_space_does_not_support_it(make):
    build, call, _ = make
    respx.get(f"{API_URL}/healthcheck").mock(
        return_value=httpx.Response(200, json={"message": "ok"}, headers=OLD_SPACE)
    )
    route = respx.post(FEATURE_URL).mock(return_value=ok())
    client = build()

    with pytest.raises(SpaceFeatureUnsupportedError) as raised:
        call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}, idempotency_key="order-42"))

    assert raised.value.capability == "idempotency-key"
    assert raised.value.written is False
    assert route.call_count == 0


# -- retries ------------------------------------------------------------------


@respx.mock
def test_no_retries_by_default(make):
    build, call, _ = make
    route = respx.post(FEATURE_URL).mock(return_value=httpx.Response(503, json={"error": "down"}, headers=CAPABILITIES))
    client = build()

    with pytest.raises(SpaceApiError) as raised:
        call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}))

    assert raised.value.status_code == 503
    assert route.call_count == 1


@respx.mock
def test_a_5xx_is_retried_with_the_same_key(make):
    build, call, _ = make
    route = respx.post(FEATURE_URL).mock(
        side_effect=[httpx.Response(502, json={"error": "bad gateway"}, headers=CAPABILITIES), ok()]
    )
    client = build(retries=2)

    result = call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}))

    assert result.consumption_id == EVALUATION["consumptionId"]
    first, second = keys(route)
    assert UUID_V4.match(first or "")
    assert second == first


@respx.mock
def test_a_network_error_is_retried_with_the_same_key_and_body(make):
    build, call, _ = make
    respx.get(f"{API_URL}/healthcheck").mock(
        return_value=httpx.Response(200, json={"message": "ok"}, headers=CAPABILITIES)
    )
    route = respx.post(FEATURE_URL).mock(side_effect=[httpx.ConnectError("reset"), ok()])
    client = build(retries=1)

    call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}, idempotency_key="order-7"))

    assert keys(route) == ["order-7", "order-7"]
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert bodies[0] == bodies[1] == {"acme-maxPets": 1}


@respx.mock
def test_capabilities_are_read_from_the_healthcheck_before_retrying_a_network_error(make):
    build, call, _ = make
    health = respx.get(f"{API_URL}/healthcheck").mock(
        return_value=httpx.Response(200, json={"message": "ok"}, headers=CAPABILITIES)
    )
    route = respx.post(FEATURE_URL).mock(side_effect=[httpx.ReadTimeout("slow"), ok()])
    client = build(retries=1)

    call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}))

    assert health.call_count == 1
    assert route.call_count == 2


@respx.mock
def test_no_retry_against_a_space_without_idempotency_key(make):
    # An older Space ignores the header: a retry could consume twice.
    build, call, _ = make
    route = respx.post(FEATURE_URL).mock(return_value=httpx.Response(503, json={"error": "down"}, headers=OLD_SPACE))
    client = build(retries=3)

    with pytest.raises(SpaceApiError):
        call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}))
    assert route.call_count == 1


@respx.mock
def test_a_network_error_that_is_not_retried_still_raises_space_connection_error(make):
    build, call, _ = make
    respx.get(f"{API_URL}/healthcheck").mock(side_effect=httpx.ConnectError("down"))
    route = respx.post(FEATURE_URL).mock(side_effect=httpx.ConnectError("down"))
    client = build(retries=2)

    with pytest.raises(SpaceConnectionError):
        call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}))
    assert route.call_count == 1


@respx.mock
def test_retry_after_is_honoured_on_in_progress_and_the_key_kept(make):
    build, call, sleeps = make
    route = respx.post(FEATURE_URL).mock(side_effect=[in_progress(), ok(**{"Idempotent-Replayed": "true"})])
    client = build(retries=2, retry_base_delay_ms=10)

    result = call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}))

    assert result.consumption_id == EVALUATION["consumptionId"]
    assert sleeps == [1.0]
    first, second = keys(route)
    assert second == first


@respx.mock
def test_in_progress_once_the_retries_are_spent(make):
    build, call, sleeps = make
    healthcheck()
    route = respx.post(FEATURE_URL).mock(return_value=in_progress())
    client = build(retries=2)

    with pytest.raises(IdempotencyInProgressError) as raised:
        call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}, idempotency_key="order-9"))

    error = raised.value
    assert isinstance(error, IdempotencyError) and isinstance(error, SpaceApiError)
    assert error.code == "IDEMPOTENCY_IN_PROGRESS"
    assert error.status_code == 409
    assert error.retry_after == 1
    assert error.idempotency_key == "order-9"
    assert route.call_count == 3
    assert sleeps == [1.0, 1.0]


@respx.mock
def test_a_retry_after_longer_than_the_maximum_ends_the_retries(make):
    build, call, sleeps = make
    route = respx.post(FEATURE_URL).mock(return_value=in_progress("120"))
    client = build(retries=2, retry_max_delay_ms=5000)

    with pytest.raises(IdempotencyInProgressError) as raised:
        call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}))

    assert raised.value.retry_after == 120
    assert route.call_count == 1
    assert sleeps == []


@respx.mock
def test_backoff_is_exponential_and_bounded(make, monkeypatch):
    build, call, sleeps = make
    monkeypatch.setattr("random.random", lambda: 0.999999)
    route = respx.post(FEATURE_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}, headers=CAPABILITIES))
    client = build(retries=4, retry_base_delay_ms=100, retry_max_delay_ms=300)

    with pytest.raises(SpaceApiError):
        call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}))

    assert route.call_count == 5
    assert [round(s, 3) for s in sleeps] == [0.1, 0.2, 0.3, 0.3]


@respx.mock
def test_conflict_is_typed_and_not_retried(make):
    build, call, _ = make
    healthcheck()
    route = respx.post(FEATURE_URL).mock(
        return_value=httpx.Response(
            409, json={"error": "Idempotency-Key reused", "code": "IDEMPOTENCY_CONFLICT"}, headers=CAPABILITIES
        )
    )
    client = build(retries=3)

    with pytest.raises(IdempotencyConflictError) as raised:
        call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 2}, idempotency_key="order-1"))

    assert raised.value.code == "IDEMPOTENCY_CONFLICT"
    assert raised.value.idempotency_key == "order-1"
    assert raised.value.status_code == 409
    assert route.call_count == 1


@respx.mock
def test_key_invalid_is_typed(make):
    build, call, _ = make
    route = respx.post(FEATURE_URL).mock(
        return_value=httpx.Response(
            400,
            json={"error": "Idempotency-Key must have between 1 and 255 characters", "code": "IDEMPOTENCY_KEY_INVALID"},
            headers=CAPABILITIES,
        )
    )
    client = build(retries=3)

    with pytest.raises(IdempotencyKeyInvalidError):
        call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}))
    assert route.call_count == 1


@respx.mock
def test_other_4xx_are_never_retried(make):
    build, call, _ = make
    feature = respx.post(FEATURE_URL).mock(
        return_value=httpx.Response(409, json={"error": "The contract kept changing"}, headers=CAPABILITIES)
    )
    contracts = respx.post(f"{API_URL}/contracts").mock(
        return_value=httpx.Response(400, json={"error": "Invalid plan"}, headers=CAPABILITIES)
    )
    client = build(retries=3)

    with pytest.raises(SpaceApiError) as raised:
        call(client.features.evaluate("u1", "acme-pets", {"acme-maxPets": 1}))
    assert type(raised.value) is SpaceApiError
    with pytest.raises(SpaceApiError):
        call(client.contracts.add_contract(contract_to_create()))
    assert feature.call_count == 1
    assert contracts.call_count == 1


@respx.mock
def test_requests_without_a_key_are_never_retried(make):
    build, call, _ = make
    route = respx.get(f"{API_URL}/contracts/u1/pricings").mock(
        return_value=httpx.Response(503, json={"error": "down"}, headers=CAPABILITIES)
    )
    client = build(retries=3)

    with pytest.raises(SpaceApiError):
        call(client.contracts.get_pricings("u1"))
    assert route.call_count == 1


@pytest.mark.parametrize("retries", [-1, 11])
def test_invalid_retries_are_rejected(retries):
    with pytest.raises(ValueError):
        SpaceClient(SpaceConnectionOptions(url=SPACE_URL, api_key="k", retries=retries))


# -- revert by consumption id -------------------------------------------------


@respx.mock
def test_revert_consumption_by_id(make):
    build, call, _ = make
    route = respx.post(url__startswith=FEATURE_URL).mock(return_value=httpx.Response(204, headers=CAPABILITIES))
    client = build()
    # The capabilities are known from the consumption.
    respx.get(f"{API_URL}/healthcheck").mock(
        return_value=httpx.Response(200, json={"message": "ok"}, headers=CAPABILITIES)
    )

    assert call(client.features.revert_consumption("u1", "acme-pets", EVALUATION["consumptionId"])) is True

    request = route.calls.last.request
    assert request.url.params["revert"] == "true"
    assert request.url.params["consumptionId"] == EVALUATION["consumptionId"]
    assert "latest" not in request.url.params
    assert UUID_V4.match(request.headers.get("idempotency-key") or "")


@respx.mock
def test_revert_consumption_answers_false_on_404(make):
    build, call, _ = make
    respx.get(f"{API_URL}/healthcheck").mock(
        return_value=httpx.Response(200, json={"message": "ok"}, headers=CAPABILITIES)
    )
    respx.post(url__startswith=FEATURE_URL).mock(
        return_value=httpx.Response(404, json={"error": "Consumption not found"}, headers=CAPABILITIES)
    )
    client = build()

    assert call(client.features.revert_consumption("u1", "acme-pets", "unknown")) is False


@respx.mock
def test_revert_consumption_is_retried_with_the_same_key(make):
    build, call, _ = make
    respx.get(f"{API_URL}/healthcheck").mock(
        return_value=httpx.Response(200, json={"message": "ok"}, headers=CAPABILITIES)
    )
    route = respx.post(url__startswith=FEATURE_URL).mock(
        side_effect=[httpx.Response(503, json={"error": "down"}, headers=CAPABILITIES), httpx.Response(204, headers=CAPABILITIES)]
    )
    client = build(retries=1)

    assert call(client.features.revert_consumption("u1", "acme-pets", "c-1", idempotency_key="undo-c-1")) is True
    assert keys(route) == ["undo-c-1", "undo-c-1"]


@respx.mock
def test_revert_consumption_needs_atomic_usage_limits(make):
    # An older Space ignores consumptionId and restores a snapshot of the level.
    build, call, _ = make
    respx.get(f"{API_URL}/healthcheck").mock(
        return_value=httpx.Response(200, json={"message": "ok"}, headers={"X-Space-Capabilities": "idempotency-key"})
    )
    route = respx.post(url__startswith=FEATURE_URL).mock(return_value=httpx.Response(204))
    client = build()

    with pytest.raises(SpaceFeatureUnsupportedError) as raised:
        call(client.features.revert_consumption("u1", "acme-pets", "c-1"))

    assert raised.value.capability == "atomic-usage-limits"
    assert route.call_count == 0


@respx.mock
def test_revert_consumption_rejects_an_empty_id(make):
    build, call, _ = make
    client = build()
    with pytest.raises(ValueError):
        call(client.features.revert_consumption("u1", "acme-pets", ""))


@respx.mock
def test_deprecated_revert_goes_to_the_feature_route_with_a_key(make):
    build, call, _ = make
    route = respx.post(url__startswith=FEATURE_URL).mock(return_value=httpx.Response(204))
    client = build()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert call(client.features.revert_evaluation("u1", "acme-pets", False)) is True

    assert any(issubclass(w.category, DeprecationWarning) for w in caught)
    request = route.calls.last.request
    assert request.url.path == "/api/v1/features/u1/acme-pets"
    assert request.url.params["revert"] == "true"
    assert request.url.params["latest"] == "false"
    assert UUID_V4.match(request.headers.get("idempotency-key") or "")


# -- contracts ----------------------------------------------------------------


def contract_to_create() -> ContractToCreate:
    return ContractToCreate(
        user_contact=UserContact(user_id="u1", username="u1"),
        billing_period=BillingPeriodToCreate(auto_renew=True, renewal_days=30),
        contracted_services={"acme": "2025-01-10"},
        subscription_plans={"acme": "pro"},
        subscription_add_ons={},
    )


@respx.mock
def test_add_contract_sends_a_key_and_reuses_it_on_a_retry(make):
    build, call, _ = make
    route = respx.post(f"{API_URL}/contracts").mock(
        side_effect=[httpx.Response(504, json={"error": "timeout"}, headers=CAPABILITIES), ok(CONTRACT, 201)]
    )
    client = build(retries=1)

    contract = call(client.contracts.add_contract(contract_to_create()))

    assert contract.user_contact.user_id == "u1"
    first, second = keys(route)
    assert UUID_V4.match(first or "")
    assert second == first


@respx.mock
def test_add_contract_with_the_callers_key(make):
    build, call, _ = make
    respx.get(f"{API_URL}/healthcheck").mock(
        return_value=httpx.Response(200, json={"message": "ok"}, headers=CAPABILITIES)
    )
    route = respx.post(f"{API_URL}/contracts").mock(return_value=ok(CONTRACT, 201, **{"Idempotent-Replayed": "true"}))
    client = build()

    call(client.contracts.add_contract(contract_to_create(), idempotency_key="signup-u1"))

    assert keys(route) == ["signup-u1"]


@respx.mock
def test_novation_sends_a_key_and_reuses_it_on_a_retry(make):
    build, call, sleeps = make
    respx.get(f"{API_URL}/healthcheck").mock(
        return_value=httpx.Response(200, json={"message": "ok"}, headers=CAPABILITIES)
    )
    route = respx.put(f"{API_URL}/contracts/u1").mock(side_effect=[in_progress(), ok(CONTRACT, ETag='"2"')])
    client = build(retries=1)

    call(client.contracts.update_contract_subscription("u1", SUBSCRIPTION, idempotency_key="change-plan-u1"))

    assert keys(route) == ["change-plan-u1", "change-plan-u1"]
    assert sleeps == [1.0]


@respx.mock
def test_a_guarded_novation_keeps_if_match_and_the_key_on_every_attempt(make):
    build, call, _ = make
    respx.get(f"{API_URL}/contracts/u1").mock(
        return_value=httpx.Response(200, json=CONTRACT, headers={**CAPABILITIES, "ETag": '"1"'})
    )
    route = respx.put(f"{API_URL}/contracts/u1").mock(
        side_effect=[httpx.Response(503, json={"error": "down"}, headers=CAPABILITIES), ok(CONTRACT, ETag='"2"')]
    )
    client = build(retries=1)

    call(client.contracts.update_contract_subscription("u1", SUBSCRIPTION, expected_contract_version="1"))

    assert [c.request.headers.get("if-match") for c in route.calls] == ['"1"', '"1"']
    first, second = keys(route)
    assert UUID_V4.match(first or "") and second == first


@respx.mock
def test_group_novation_and_usage_levels_send_keys(make):
    build, call, _ = make
    group = respx.put(f"{API_URL}/contracts", params={"groupId": "g1"}).mock(return_value=ok([CONTRACT]))
    levels = respx.put(f"{API_URL}/contracts/u1/usageLevels").mock(
        side_effect=[httpx.Response(500, json={"error": "boom"}, headers=CAPABILITIES), ok(CONTRACT)]
    )
    client = build(retries=1)

    call(client.contracts.update_contract_subscription_by_group_id("g1", SUBSCRIPTION))
    call(client.contracts.update_contract_usage_levels("u1", "acme", {"maxPets": 1}))

    assert UUID_V4.match(keys(group)[0] or "")
    first, second = keys(levels)
    assert UUID_V4.match(first or "") and second == first


# -- capabilities -------------------------------------------------------------


@respx.mock
def test_has_capability_reads_the_healthcheck_once(make):
    build, call, _ = make
    health = respx.get(f"{API_URL}/healthcheck").mock(
        return_value=httpx.Response(200, json={"message": "ok"}, headers=CAPABILITIES)
    )
    client = build()

    assert call(client.has_capability("idempotency-key")) is True
    assert call(client.has_capability("atomic-usage-limits")) is True
    assert call(client.has_capability("something-else")) is False
    assert health.call_count == 1
