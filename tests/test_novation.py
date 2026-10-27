"""Changing a subscription safely: S4, S5 and S8 of the API design.

* ``contracts.preview_novation`` - a dry run that writes nothing.
* ``contracts.update_contract_subscription(..., expected_contract_version=...)``
  - optimistic concurrency, ``ContractChangedError`` on 412.
* ``keep_billing_period`` on both.

Every test runs against both clients (see the ``space`` fixture in conftest).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx
from conftest import API_URL, SPACE_URL

from space_client import (
    AsyncSpaceClient,
    ContractChangedError,
    SpaceApiError,
    SpaceClient,
)
from space_client.types import (
    CacheOptions,
    NovationErrorCode,
    NovationPreview,
    SpaceConnectionOptions,
    Subscription,
)

CONTRACT = {
    "userContact": {"userId": "u1", "username": "u1"},
    "billingPeriod": {"startDate": "2026-10-01", "endDate": "2026-11-01", "autoRenew": True, "renewalDays": 30},
    "usageLevels": {},
    "contractedServices": {"acme": "2025-01-10"},
    "subscriptionPlans": {"acme": "pro"},
    "subscriptionAddOns": {"acme": {"extraStorage": 1}},
    "history": [],
    "contractVersion": "4:2026-10-27T10:05:00.000Z",
}

PREVIEW = {
    "valid": False,
    "errors": [
        {
            "code": "ADDON_NOT_AVAILABLE",
            "service": "acme",
            "path": "subscriptionAddOns.acme.extraStorage",
            "message": "extraStorage is not available for plan basic",
        },
        {"code": "SOMETHING_NEW", "service": "acme", "message": "a code this client does not know"},
    ],
    "effective": {
        "contractedServices": {"acme": "2025-01-10"},
        "subscriptionPlans": {"acme": "basic"},
        "subscriptionAddOns": {"acme": {"extraStorage": 2}},
    },
    "versionChanges": {"acme": {"from": "2024-06-08", "to": "2025-01-10", "reason": "policy:all_last"}},
    "usageWarnings": [{"service": "acme", "usageLimit": "storage", "consumed": 42, "newLimit": 10}],
    "billingPeriod": {"startDate": "2026-10-27", "endDate": "2026-11-26", "restarts": True},
    "contractVersion": "3:2026-10-27T10:00:00.000Z",
}

CHANGE = Subscription(
    contracted_services={"acme": "2025-01-10"},
    subscription_plans={"acme": "basic"},
    subscription_add_ons={"acme": {"extraStorage": 2}},
)


def cached_client(flavour: str):
    options = SpaceConnectionOptions(url=SPACE_URL, api_key="k", cache=CacheOptions(enabled=True))
    return AsyncSpaceClient(options) if flavour == "async" else SpaceClient(options)


def body_of(route) -> dict:
    return json.loads(route.calls.last.request.content)


# -- S4: preview -------------------------------------------------------------


@respx.mock
def test_preview_novation_parses_the_whole_answer(space):
    client, call = space
    route = respx.post(f"{API_URL}/contracts/u1/novation/preview").mock(
        return_value=httpx.Response(200, json=PREVIEW)
    )

    preview = call(client.contracts.preview_novation("u1", CHANGE))

    assert isinstance(preview, NovationPreview)
    assert body_of(route) == CHANGE.to_dict()
    assert "keepBillingPeriod" not in str(route.calls.last.request.url)

    # An invalid change is an answer, not an exception.
    assert preview.valid is False
    first = preview.errors[0]
    assert first.code == NovationErrorCode.ADDON_NOT_AVAILABLE
    assert first.service == "acme"
    assert first.path == "subscriptionAddOns.acme.extraStorage"
    # Unknown codes are kept as strings rather than dropped or rejected.
    assert preview.errors[1].code == "SOMETHING_NEW"
    assert preview.errors[1].path is None

    assert preview.effective.subscription_plans == {"acme": "basic"}
    assert preview.effective.subscription_add_ons == {"acme": {"extraStorage": 2}}
    change = preview.version_changes["acme"]
    assert (change.from_version, change.to_version, change.reason) == (
        "2024-06-08",
        "2025-01-10",
        "policy:all_last",
    )
    warning = preview.usage_warnings[0]
    assert (warning.service, warning.usage_limit, warning.consumed, warning.new_limit) == (
        "acme",
        "storage",
        42,
        10,
    )
    assert preview.billing_period.restarts is True
    assert preview.billing_period.start_date == "2026-10-27"
    assert preview.contract_version == "3:2026-10-27T10:00:00.000Z"


@respx.mock
def test_preview_omits_empty_contracted_services_and_can_keep_the_billing_period(space):
    client, call = space
    route = respx.post(f"{API_URL}/contracts/u1/novation/preview").mock(
        return_value=httpx.Response(200, json={**PREVIEW, "valid": True, "errors": []})
    )

    preview = call(
        client.contracts.preview_novation(
            "u1",
            Subscription(subscription_plans={"acme": "basic"}),
            keep_billing_period=True,
        )
    )

    assert preview.valid is True and preview.errors == []
    # "Keep the versions or let the policy decide" is said by leaving them out.
    assert "contractedServices" not in body_of(route)
    assert route.calls.last.request.url.params["keepBillingPeriod"] == "true"


@respx.mock
def test_preview_writes_nothing_so_it_does_not_touch_the_cache(flavour):
    client = cached_client(flavour)
    client.cache.set(client.cache.get_contract_key("u1"), CONTRACT)
    respx.post(f"{API_URL}/contracts/u1/novation/preview").mock(
        return_value=httpx.Response(200, json=PREVIEW)
    )

    result = client.contracts.preview_novation("u1", CHANGE)
    if flavour == "async":
        asyncio.run(result)

    assert client.cache.get(client.cache.get_contract_key("u1")) == CONTRACT
    client.cache.close()


# -- S5: optimistic concurrency ----------------------------------------------


@respx.mock
def test_update_with_an_expected_version_sends_if_match(space):
    client, call = space
    route = respx.put(f"{API_URL}/contracts/u1").mock(
        return_value=httpx.Response(200, json=CONTRACT, headers={"ETag": '"4:2026-10-27T10:05:00.000Z"'})
    )

    contract = call(
        client.contracts.update_contract_subscription(
            "u1", CHANGE, expected_contract_version="3:2026-10-27T10:00:00.000Z"
        )
    )

    request = route.calls.last.request
    assert request.headers["If-Match"] == '"3:2026-10-27T10:00:00.000Z"'
    assert json.loads(request.content) == CHANGE.to_dict()
    assert "keepBillingPeriod" not in str(request.url)
    assert contract.contract_version == "4:2026-10-27T10:05:00.000Z"


@respx.mock
def test_a_concurrent_change_raises_contract_changed_error(space):
    client, call = space
    respx.put(f"{API_URL}/contracts/u1").mock(
        return_value=httpx.Response(
            412, json={"error": "CONTRACT_CHANGED", "contractVersion": "5:2026-10-27T10:06:00.000Z"}
        )
    )

    with pytest.raises(ContractChangedError) as caught:
        call(
            client.contracts.update_contract_subscription(
                "u1", CHANGE, expected_contract_version="3:old"
            )
        )

    error = caught.value
    assert isinstance(error, SpaceApiError) and error.status_code == 412
    assert error.user_id == "u1"
    assert error.expected_contract_version == "3:old"
    assert error.contract_version == "5:2026-10-27T10:06:00.000Z"
    assert "changed" in str(error)


@respx.mock
def test_a_412_invalidates_what_the_cache_held_for_the_user(flavour):
    client = cached_client(flavour)
    client.cache.set(client.cache.get_contract_key("u1"), CONTRACT)
    respx.put(f"{API_URL}/contracts/u1").mock(
        return_value=httpx.Response(412, json={"error": "CONTRACT_CHANGED", "contractVersion": "9"})
    )

    with pytest.raises(ContractChangedError):
        result = client.contracts.update_contract_subscription("u1", CHANGE, expected_contract_version="3")
        if flavour == "async":
            asyncio.run(result)

    assert client.cache.get(client.cache.get_contract_key("u1")) is None
    client.cache.close()


@respx.mock
def test_without_an_expected_version_the_update_is_unconditional_as_before(space):
    client, call = space
    route = respx.put(f"{API_URL}/contracts/u1").mock(return_value=httpx.Response(200, json=CONTRACT))

    call(client.contracts.update_contract_subscription("u1", CHANGE))

    request = route.calls.last.request
    assert "If-Match" not in request.headers
    assert str(request.url) == f"{API_URL}/contracts/u1"


@respx.mock
def test_other_refusals_of_an_update_stay_generic(space):
    client, call = space
    respx.put(f"{API_URL}/contracts/u1").mock(
        return_value=httpx.Response(400, json={"error": "PRIVATE_PLAN"})
    )

    with pytest.raises(SpaceApiError) as caught:
        call(client.contracts.update_contract_subscription("u1", CHANGE, expected_contract_version="3"))
    assert not isinstance(caught.value, ContractChangedError)


# -- S8: keepBillingPeriod ---------------------------------------------------


@respx.mock
def test_update_can_keep_the_billing_period(space):
    client, call = space
    route = respx.put(f"{API_URL}/contracts/u1", params={"keepBillingPeriod": "true"}).mock(
        return_value=httpx.Response(200, json=CONTRACT)
    )

    contract = call(
        client.contracts.update_contract_subscription(
            "u1", CHANGE, expected_contract_version="3", keep_billing_period=True
        )
    )

    assert route.called
    assert contract.billing_period.start_date == "2026-10-01"


def test_contract_version_round_trips_through_the_model():
    from space_client.types import Contract

    contract = Contract.from_dict(CONTRACT)
    assert contract.contract_version == "4:2026-10-27T10:05:00.000Z"
    assert Contract.from_dict(contract.to_dict()).contract_version == contract.contract_version
    # Older servers do not send it.
    assert Contract.from_dict({"userContact": {"userId": "u", "username": "u"}}).contract_version is None
