"""Reading the pricing a pricing page needs: S2, S3 and S13 of the API design.

* ``pricings.get_yaml`` - the raw Pricing2Yaml of one version, with ETag.
* ``contracts.get_pricings`` - the version each service of a contract is on.
* ``services.get_new_contracts_pricing`` - the version new contracts get.

Every test runs against both clients (see the ``space`` fixture in conftest).
"""

from __future__ import annotations

import httpx
import pytest
import respx
from conftest import API_URL

from space_client import PricingYamlUnavailableError, SpaceApiError
from space_client.types import ContractPricings, NewContractsPricing, PricingYaml

YAML = (
    "syntaxVersion: '3.0'\n"
    "saasName: Acme\n"
    "version: '2024-06-08'\n"
    "# comments and key order survive: the file is returned byte for byte\n"
    "plans:\n  growth:\n    price: 20\n"
)
ETAG = '"5d41402abc4b2a76b9719d911017c592"'


# -- S2: pricings.get_yaml ---------------------------------------------------


@respx.mock
def test_get_yaml_returns_the_file_byte_for_byte_with_its_etag(space):
    client, call = space
    route = respx.get(f"{API_URL}/services/acme/pricings/2024-06-08", params={"format": "yaml"}).mock(
        return_value=httpx.Response(
            200,
            content=YAML.encode(),
            headers={
                "Content-Type": "application/yaml; charset=utf-8",
                "ETag": ETAG,
                "X-Pricing-Syntax-Version": "3.0",
            },
        )
    )

    result = call(client.pricings.get_yaml("acme", "2024-06-08"))

    assert isinstance(result, PricingYaml)
    assert result.yaml == YAML
    assert result.etag == ETAG
    assert result.syntax_version == "3.0"
    assert result.not_modified is False
    request = route.calls.last.request
    assert request.headers["x-api-key"] == "org-key"
    assert "If-None-Match" not in request.headers


@respx.mock
def test_get_yaml_with_a_known_etag_is_a_conditional_request(space):
    client, call = space
    route = respx.get(f"{API_URL}/services/acme/pricings/2024-06-08").mock(
        return_value=httpx.Response(304, headers={"ETag": ETAG})
    )

    # Unquoted etags are quoted, so callers can pass either form.
    result = call(client.pricings.get_yaml("acme", "2024-06-08", etag=ETAG.strip('"')))

    assert route.calls.last.request.headers["If-None-Match"] == ETAG
    assert result.not_modified is True
    assert result.yaml is None
    assert result.etag == ETAG


@respx.mock
def test_get_yaml_escapes_path_segments(space):
    client, call = space
    route = respx.get(url__regex=r".*/services/my%20svc/pricings/v1%2F2\?format=yaml$").mock(
        return_value=httpx.Response(200, content=b"x: 1\n")
    )

    result = call(client.pricings.get_yaml("my svc", "v1/2"))

    assert route.called
    assert result.yaml == "x: 1\n"
    assert result.etag is None and result.syntax_version is None


@respx.mock
def test_get_yaml_unavailable_raises_a_typed_error(space):
    client, call = space
    respx.get(f"{API_URL}/services/acme/pricings/2019").mock(
        return_value=httpx.Response(404, json={"error": "PRICING_YAML_UNAVAILABLE"})
    )

    with pytest.raises(PricingYamlUnavailableError) as caught:
        call(client.pricings.get_yaml("acme", "2019"))

    # Still a SpaceApiError, so existing `except SpaceApiError` blocks keep working.
    assert isinstance(caught.value, SpaceApiError)
    assert caught.value.status_code == 404
    assert caught.value.service_name == "acme"
    assert caught.value.pricing_version == "2019"


@respx.mock
def test_get_yaml_of_an_unknown_version_is_a_plain_refusal(space):
    client, call = space
    respx.get(f"{API_URL}/services/acme/pricings/nope").mock(
        return_value=httpx.Response(404, json={"error": "Pricing not found"})
    )

    with pytest.raises(SpaceApiError) as caught:
        call(client.pricings.get_yaml("acme", "nope"))

    assert not isinstance(caught.value, PricingYamlUnavailableError)


# -- S3: contracts.get_pricings ----------------------------------------------


CONTRACT_PRICINGS = {
    "services": {
        "acme": {
            "version": "2024-06-08",
            "status": "archived",
            "yamlUrl": "/api/v1/services/acme/pricings/2024-06-08?format=yaml",
            "newContractsVersion": "2025-01-10",
            "plan": "growth",
            "addOns": {"extraStorage": 2},
        },
        "beta": {
            "version": "1.0",
            "status": "active",
            "yamlUrl": "/api/v1/services/beta/pricings/1.0?format=yaml",
            "newContractsVersion": None,
        },
    },
    "contractVersion": "3:2026-10-27T10:00:00.000Z",
}


@respx.mock
def test_get_pricings_of_a_contract(space):
    client, call = space
    route = respx.get(f"{API_URL}/contracts/user%2F1/pricings").mock(
        return_value=httpx.Response(200, json=CONTRACT_PRICINGS)
    )

    result = call(client.contracts.get_pricings("user/1"))

    assert route.called
    assert isinstance(result, ContractPricings)
    assert result.contract_version == "3:2026-10-27T10:00:00.000Z"
    acme = result.services["acme"]
    assert acme.version == "2024-06-08"
    assert acme.status == "archived"
    assert acme.yaml_url.endswith("?format=yaml")
    assert acme.new_contracts_version == "2025-01-10"
    assert acme.plan == "growth"
    assert acme.add_ons == {"extraStorage": 2}
    beta = result.services["beta"]
    assert beta.new_contracts_version is None
    assert beta.plan is None
    assert beta.add_ons == {}


@respx.mock
def test_get_pricings_of_a_contract_outside_the_org_is_a_404(space):
    client, call = space
    respx.get(f"{API_URL}/contracts/other/pricings").mock(
        return_value=httpx.Response(404, json={"error": "Contract not found"})
    )

    with pytest.raises(SpaceApiError) as caught:
        call(client.contracts.get_pricings("other"))
    assert caught.value.status_code == 404


# -- S13: services.get_new_contracts_pricing ---------------------------------


@respx.mock
def test_get_new_contracts_pricing(space):
    client, call = space
    respx.get(f"{API_URL}/services/acme/pricings/new-contracts").mock(
        return_value=httpx.Response(
            200,
            json={
                "version": "2025-01-10",
                "yamlUrl": "/api/v1/services/acme/pricings/2025-01-10?format=yaml",
                "source": "sphere-policy",
            },
        )
    )

    result = call(client.services.get_new_contracts_pricing("acme"))

    assert isinstance(result, NewContractsPricing)
    assert result.version == "2025-01-10"
    assert result.yaml_url == "/api/v1/services/acme/pricings/2025-01-10?format=yaml"
    assert result.source == "sphere-policy"


def test_the_new_modules_exist_on_both_clients_with_the_same_calls():
    from space_client.modules.pricing_module import (
        AsyncPricingModule,
        PricingModule,
        _PricingCalls,
    )
    from space_client.modules.service_module import (
        AsyncServiceModule,
        ServiceModule,
        _ServiceCalls,
    )

    for shared, sync_module, async_module in (
        (_PricingCalls, PricingModule, AsyncPricingModule),
        (_ServiceCalls, ServiceModule, AsyncServiceModule),
    ):
        declared = {name for name in vars(shared) if not name.startswith("_")}
        assert declared
        for name in declared:
            assert hasattr(sync_module, name) and hasattr(async_module, name)
