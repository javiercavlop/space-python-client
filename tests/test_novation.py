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
    SpaceError,
    SpaceFeatureUnsupportedError,
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


# What a Space with S0, S5 and S8 says about itself on every response.
CAPABILITIES = {"X-Space-Capabilities": "contract-org-scope, contract-version,keep-billing-period"}
CURRENT_ETAG = '"4:2026-10-27T10:05:00.000Z"'


def new_space_contract(etag: str = CURRENT_ETAG, capabilities: dict | None = None, body: dict | None = None):
    """``GET /contracts/u1`` on a Space that supports the novation options."""
    headers = dict(CAPABILITIES if capabilities is None else capabilities)
    if etag:
        headers["ETag"] = etag
    return respx.get(f"{API_URL}/contracts/u1").mock(
        return_value=httpx.Response(200, json=body or CONTRACT, headers=headers)
    )


def put_answer(status: int = 200, body: dict | None = None, etag: str | None = '"5:new"', capabilities: dict | None = None):
    headers = dict(CAPABILITIES if capabilities is None else capabilities)
    if etag:
        headers["ETag"] = etag
    return httpx.Response(status, json=CONTRACT if body is None else body, headers=headers)


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
        return_value=httpx.Response(200, json={**PREVIEW, "valid": True, "errors": []}, headers=CAPABILITIES)
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
    new_space_contract(etag='"3:2026-10-27T10:00:00.000Z"')
    route = respx.put(f"{API_URL}/contracts/u1").mock(
        return_value=put_answer(body={**CONTRACT, "contractVersion": None}, etag=CURRENT_ETAG)
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
    new_space_contract(etag='"3:old"')
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
    new_space_contract(etag='"3"')
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
    probe = respx.get(f"{API_URL}/contracts/u1").mock(return_value=httpx.Response(200, json=CONTRACT))
    # An old Space: no capabilities, no ETag. Without options nothing is checked.
    route = respx.put(f"{API_URL}/contracts/u1").mock(return_value=httpx.Response(200, json=CONTRACT))

    call(client.contracts.update_contract_subscription("u1", CHANGE))

    request = route.calls.last.request
    assert "If-Match" not in request.headers
    assert str(request.url) == f"{API_URL}/contracts/u1"
    # One request, the same as before: no capability check, no read first.
    assert route.call_count == 1 and not probe.called


@respx.mock
def test_other_refusals_of_an_update_stay_generic(space):
    client, call = space
    new_space_contract(etag='"3"')
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
    new_space_contract(etag='"3"')
    route = respx.put(f"{API_URL}/contracts/u1", params={"keepBillingPeriod": "true"}).mock(
        return_value=put_answer()
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
    # Older servers do not send it, and then it is not sent back either.
    unknown = Contract.from_dict({"userContact": {"userId": "u", "username": "u"}})
    assert unknown.contract_version is None
    assert "contractVersion" not in unknown.to_dict()


# -- S0: capabilities, and what an older Space does with the options ---------
#
# A Space without S5 ignores If-Match and one without S8 ignores
# keepBillingPeriod: the novation would apply unconditionally, or restart the
# billing period, while the caller believes otherwise. The client checks
# X-Space-Capabilities before writing, and the answer after writing.


@pytest.mark.parametrize("version", [None, ""])
@respx.mock
def test_an_explicit_empty_expected_version_is_rejected_before_sending(space, version):
    client, call = space
    probe = respx.get(f"{API_URL}/contracts/u1").mock(return_value=httpx.Response(200, json=CONTRACT))
    route = respx.put(f"{API_URL}/contracts/u1").mock(return_value=put_answer())

    # Raised by the call (for the async client, when it is awaited).
    with pytest.raises(ValueError, match="expected_contract_version"):
        call(client.contracts.update_contract_subscription("u1", CHANGE, expected_contract_version=version))

    assert not route.called and not probe.called


@pytest.mark.parametrize(
    ("kwargs", "capability"),
    [
        ({"expected_contract_version": "3"}, "contract-version"),
        ({"keep_billing_period": True}, "keep-billing-period"),
        ({"expected_contract_version": "3", "keep_billing_period": True}, "contract-version"),
    ],
)
@respx.mock
def test_an_old_space_without_the_capability_header_gets_no_write(space, kwargs, capability):
    client, call = space
    # SPACE 1.7: no X-Space-Capabilities, no ETag, no contractVersion.
    old_contract = {k: v for k, v in CONTRACT.items() if k != "contractVersion"}
    probe = respx.get(f"{API_URL}/contracts/u1").mock(return_value=httpx.Response(200, json=old_contract))
    route = respx.put(url__startswith=f"{API_URL}/contracts/u1").mock(
        return_value=httpx.Response(200, json=old_contract)
    )

    with pytest.raises(SpaceFeatureUnsupportedError) as caught:
        call(client.contracts.update_contract_subscription("u1", CHANGE, **kwargs))

    error = caught.value
    assert error.capability == capability
    assert error.written is False and error.contract is None
    assert isinstance(error, SpaceError) and not isinstance(error, SpaceApiError)
    assert "nothing was written" in str(error)
    assert probe.call_count == 1
    assert not route.called


@respx.mock
def test_a_space_with_concurrency_but_not_keep_billing_period_gets_no_write(space):
    client, call = space
    new_space_contract(etag='"3"', capabilities={"X-Space-Capabilities": "contract-version"})
    route = respx.put(url__startswith=f"{API_URL}/contracts/u1").mock(return_value=put_answer())

    with pytest.raises(SpaceFeatureUnsupportedError) as caught:
        call(
            client.contracts.update_contract_subscription(
                "u1", CHANGE, expected_contract_version="3", keep_billing_period=True
            )
        )

    assert caught.value.capability == "keep-billing-period"
    assert not route.called


@respx.mock
def test_capabilities_are_cached_per_client_and_can_be_cleared(space):
    client, call = space
    probe = new_space_contract(etag='"5:new"')
    route = respx.put(f"{API_URL}/contracts/u1").mock(return_value=put_answer(etag='"5:new"'))

    call(client.contracts.update_contract_subscription("u1", CHANGE, expected_contract_version="5:new"))
    call(client.contracts.update_contract_subscription("u1", CHANGE, expected_contract_version="5:new"))

    # Read once, then known from the answers for CAPABILITIES_TTL_SECONDS.
    assert probe.call_count == 1 and route.call_count == 2
    assert client.CAPABILITIES_TTL_SECONDS == 60

    client.clear_capabilities()
    call(client.contracts.update_contract_subscription("u1", CHANGE, expected_contract_version="5:new"))
    assert probe.call_count == 2


@respx.mock
def test_capabilities_expire_after_the_ttl(space, monkeypatch):
    from space_client import _base_client

    client, call = space
    now = [1000.0]
    monkeypatch.setattr(_base_client, "_now", lambda: now[0])
    probe = new_space_contract(etag='"5:new"')
    respx.put(f"{API_URL}/contracts/u1").mock(return_value=put_answer(etag='"5:new"'))

    call(client.contracts.update_contract_subscription("u1", CHANGE, expected_contract_version="5:new"))
    now[0] += 59
    call(client.contracts.update_contract_subscription("u1", CHANGE, expected_contract_version="5:new"))
    assert probe.call_count == 1
    now[0] += 61
    call(client.contracts.update_contract_subscription("u1", CHANGE, expected_contract_version="5:new"))
    assert probe.call_count == 2


@respx.mock
def test_capabilities_learnt_from_any_response_avoid_the_read(space):
    client, call = space
    respx.get(f"{API_URL}/contracts/u1/pricings").mock(
        return_value=httpx.Response(200, json={"services": {}, "contractVersion": "3"}, headers=CAPABILITIES)
    )
    probe = new_space_contract()
    # A refusal from something without the header (a proxy) does not erase them.
    respx.get(f"{API_URL}/services/acme/pricings/new-contracts").mock(
        return_value=httpx.Response(502, text="Bad gateway")
    )
    route = respx.put(f"{API_URL}/contracts/u1").mock(return_value=put_answer())

    pricings = call(client.contracts.get_pricings("u1"))
    with pytest.raises(SpaceApiError):
        call(client.services.get_new_contracts_pricing("acme"))
    call(
        client.contracts.update_contract_subscription(
            "u1", CHANGE, expected_contract_version=pricings.contract_version
        )
    )

    assert not probe.called
    assert route.calls.last.request.headers["If-Match"] == '"3"'


@respx.mock
def test_a_stale_expected_version_fails_early_on_the_read(flavour):
    client = cached_client(flavour)
    client.cache.set(client.cache.get_contract_key("u1"), CONTRACT)
    new_space_contract(etag='"7:someone-else"')
    route = respx.put(f"{API_URL}/contracts/u1").mock(return_value=put_answer())

    with pytest.raises(ContractChangedError) as caught:
        result = client.contracts.update_contract_subscription("u1", CHANGE, expected_contract_version='"3"')
        if flavour == "async":
            asyncio.run(result)

    error = caught.value
    assert error.expected_contract_version == '"3"'
    assert error.contract_version == "7:someone-else"
    assert error.status_code == 412
    assert not route.called
    assert client.cache.get(client.cache.get_contract_key("u1")) is None
    client.cache.close()


@respx.mock
def test_a_space_that_claims_support_but_ignores_if_match_is_caught_after_the_write(flavour):
    # Backstop: the server advertises contract-version, applies the change and
    # answers without an ETag. The write happened; say so.
    client = cached_client(flavour)
    client.cache.set(client.cache.get_contract_key("u1"), CONTRACT)
    new_space_contract(etag='"3"')
    respx.put(f"{API_URL}/contracts/u1").mock(
        return_value=put_answer(body={**CONTRACT, "contractVersion": None}, etag=None)
    )

    with pytest.raises(SpaceFeatureUnsupportedError) as caught:
        result = client.contracts.update_contract_subscription("u1", CHANGE, expected_contract_version="3")
        if flavour == "async":
            asyncio.run(result)

    error = caught.value
    assert error.capability == "contract-version"
    assert error.written is True
    assert error.contract.subscription_plans == {"acme": "pro"}
    assert "If-Match" in str(error)
    # Nothing is kept for a user whose contract is in an unknown state.
    assert client.cache.get(client.cache.get_contract_key("u1")) is None
    client.cache.close()


@respx.mock
def test_a_space_that_ignores_keep_billing_period_is_caught_after_the_write(space):
    client, call = space
    new_space_contract(etag='"3"')
    restarted = {**CONTRACT, "billingPeriod": {**CONTRACT["billingPeriod"], "startDate": "2026-10-27"}}
    respx.put(f"{API_URL}/contracts/u1", params={"keepBillingPeriod": "true"}).mock(
        return_value=put_answer(body=restarted)
    )

    with pytest.raises(SpaceFeatureUnsupportedError) as caught:
        call(
            client.contracts.update_contract_subscription(
                "u1", CHANGE, expected_contract_version="3", keep_billing_period=True
            )
        )

    assert caught.value.capability == "keep-billing-period"
    assert caught.value.written is True
    assert caught.value.contract.billing_period.start_date == "2026-10-27"


@respx.mock
def test_the_billing_period_backstop_uses_the_history_when_nothing_was_read(space):
    client, call = space
    respx.get(f"{API_URL}/contracts/u1/pricings").mock(
        return_value=httpx.Response(200, json={"services": {}}, headers=CAPABILITIES)
    )
    call(client.contracts.get_pricings("u1"))
    # The pushed history entry holds the replaced period: same instant, other spelling.
    kept = {**CONTRACT, "history": [{"startDate": "2026-10-01T00:00:00.000Z"}]}
    restarted = {**kept, "billingPeriod": {**CONTRACT["billingPeriod"], "startDate": "2026-10-27"}}
    route = respx.put(f"{API_URL}/contracts/u1", params={"keepBillingPeriod": "true"}).mock(
        side_effect=[put_answer(body=kept), put_answer(body=restarted)]
    )

    kept_contract = call(client.contracts.update_contract_subscription("u1", CHANGE, keep_billing_period=True))
    assert kept_contract.billing_period.start_date == "2026-10-01"
    with pytest.raises(SpaceFeatureUnsupportedError) as caught:
        call(client.contracts.update_contract_subscription("u1", CHANGE, keep_billing_period=True))
    assert caught.value.written is True
    assert route.call_count == 2


@respx.mock
def test_previewing_with_keep_billing_period_on_an_old_space_raises(space):
    client, call = space
    respx.post(f"{API_URL}/contracts/u1/novation/preview").mock(
        return_value=httpx.Response(200, json=PREVIEW)
    )

    with pytest.raises(SpaceFeatureUnsupportedError) as caught:
        call(client.contracts.preview_novation("u1", CHANGE, keep_billing_period=True))
    assert caught.value.capability == "keep-billing-period"
    assert caught.value.written is False

    # Without the option an old Space's preview is still an answer.
    assert call(client.contracts.preview_novation("u1", CHANGE)).valid is False


# -- S8: rejectPrivate (opt-in) ----------------------------------------------
#
# Rejecting private plans/add-ons is opt-in on the server (`rejectPrivate=true`,
# capability `reject-private`). A Space that does not know it would accept a
# private plan without a word, so the client refuses rather than send it.

WITH_REJECT_PRIVATE = {"X-Space-Capabilities": CAPABILITIES["X-Space-Capabilities"] + ",reject-private"}


@respx.mock
def test_update_can_ask_space_to_reject_private_plans(space):
    client, call = space
    new_space_contract(etag='"3"', capabilities=WITH_REJECT_PRIVATE)
    route = respx.put(url__startswith=f"{API_URL}/contracts/u1").mock(
        return_value=put_answer(capabilities=WITH_REJECT_PRIVATE)
    )

    call(
        client.contracts.update_contract_subscription(
            "u1", CHANGE, expected_contract_version="3", keep_billing_period=True, reject_private=True
        )
    )

    params = route.calls.last.request.url.params
    assert params["rejectPrivate"] == "true"
    assert params["keepBillingPeriod"] == "true"


@respx.mock
def test_reject_private_alone_is_checked_too(space):
    client, call = space
    new_space_contract(etag='"3"', capabilities=WITH_REJECT_PRIVATE)
    route = respx.put(url__startswith=f"{API_URL}/contracts/u1").mock(
        return_value=put_answer(capabilities=WITH_REJECT_PRIVATE)
    )

    call(client.contracts.update_contract_subscription("u1", CHANGE, reject_private=True))

    request = route.calls.last.request
    assert request.url.params["rejectPrivate"] == "true"
    assert "keepBillingPeriod" not in request.url.params
    assert "If-Match" not in request.headers


@pytest.mark.parametrize("capabilities", [CAPABILITIES, {}])
@respx.mock
def test_a_space_without_reject_private_gets_no_write(space, capabilities):
    client, call = space
    # A Space with S5/S8-keepBillingPeriod but without opt-in rejection, and one
    # without any capability header at all.
    probe = respx.get(f"{API_URL}/contracts/u1").mock(
        return_value=httpx.Response(200, json=CONTRACT, headers={**capabilities, "ETag": '"3"'})
    )
    route = respx.put(url__startswith=f"{API_URL}/contracts/u1").mock(return_value=put_answer())

    with pytest.raises(SpaceFeatureUnsupportedError) as caught:
        call(client.contracts.update_contract_subscription("u1", CHANGE, reject_private=True))

    assert caught.value.capability == "reject-private"
    assert caught.value.written is False
    assert "reject_private" in str(caught.value)
    assert probe.called and not route.called


@respx.mock
def test_preview_can_reject_private_plans_on_a_space_that_supports_it(space):
    client, call = space
    route = respx.post(f"{API_URL}/contracts/u1/novation/preview").mock(
        return_value=httpx.Response(200, json=PREVIEW, headers=WITH_REJECT_PRIVATE)
    )

    preview = call(client.contracts.preview_novation("u1", CHANGE, reject_private=True))

    assert route.calls.last.request.url.params["rejectPrivate"] == "true"
    assert preview.valid is False


@respx.mock
def test_preview_with_reject_private_on_a_space_without_it_raises(space):
    client, call = space
    respx.post(f"{API_URL}/contracts/u1/novation/preview").mock(
        return_value=httpx.Response(200, json=PREVIEW, headers=CAPABILITIES)
    )

    with pytest.raises(SpaceFeatureUnsupportedError) as caught:
        call(client.contracts.preview_novation("u1", CHANGE, reject_private=True))

    assert caught.value.capability == "reject-private"
    assert caught.value.written is False


@respx.mock
def test_without_reject_private_nothing_is_sent_or_checked(space):
    client, call = space
    route = respx.post(f"{API_URL}/contracts/u1/novation/preview").mock(
        return_value=httpx.Response(200, json=PREVIEW)
    )

    call(client.contracts.preview_novation("u1", CHANGE))

    assert "rejectPrivate" not in route.calls.last.request.url.params
