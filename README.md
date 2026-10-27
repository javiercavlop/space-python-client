# Space Python Client

Python client library for [Space](https://github.com/isa-group/space), a pricing-driven self-adaptation platform for SaaS applications.

[![PyPI](https://img.shields.io/pypi/v/space-python-client.svg)](https://pypi.org/project/space-python-client)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Table of Contents

- [What You Get](#what-you-get)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick Start in 5 Minutes](#quick-start-in-5-minutes)
- [Async Client](#async-client)
- [Configuration](#configuration)
- [API Overview](#api-overview)
- [Data Models](#data-models)
- [Caching](#caching)
- [WebSocket Events](#websocket-events)
- [Error Handling](#error-handling)
- [Testing](#testing)
- [Contributing](#contributing)
- [License](#license)

## What You Get

- Simple API to connect to Space.
- An asynchronous client for services that run in an event loop.
- Contract lifecycle operations.
- Feature evaluation with optional expected consumption.
- Revert operation for optimistic usage updates.
- Pricing token generation.
- Pricing reads for pricing pages: the raw Pricing2Yaml of a version, the
  versions of a user's contract, and the version new contracts get.
- Built-in in-memory cache and optional Redis cache.
- WebSocket events for real-time pricing updates.

## Requirements

- Python 3.10+

## Installation

```bash
pip install space-python-client
```

Optional Redis support:

```bash
pip install "space-python-client[redis]"
```

## Quick Start in 5 Minutes

```python
from space_client import SpaceClientFactory

client = SpaceClientFactory.connect(
	"http://localhost:3000",
	"your-api-key",
	10_000,
)

healthy = client.is_connected_to_space()
print(f"Connected: {healthy}")

result = client.features.evaluate("user-123", "serviceA-featureX")
if result.error is not None:
	print(f"Evaluation error: {result.error.message}")
else:
	print(f"Feature enabled: {result.eval}")

pricing_token = client.features.generate_user_pricing_token("user-123")
print(f"Pricing token: {pricing_token}")

client.close()
```

## Async Client

Entitlement checks sit on the request path: a service asks Space "may this user
do this?" before serving a request. Made synchronously from inside an event
loop, that call blocks every other request the process is serving for as long as
it takes - including the full timeout, when Space is slow or unreachable.

`AsyncSpaceClient` is the same client over `httpx.AsyncClient`. Same modules,
same method names, same arguments, same exceptions; the calls are awaited.

```python
from space_client import SpaceClientFactory

client = SpaceClientFactory.connect_async(
	"http://localhost:3000",
	"your-api-key",
	10_000,
)

async with client as space:
	result = await space.features.evaluate("user-123", "serviceA-featureX")
	print(f"Feature enabled: {result.eval}")

	token = await space.features.generate_user_pricing_token("user-123")
	print(f"Pricing token: {token}")
```

Two differences, both consequences of running in a loop:

- **The WebSocket is not opened by the constructor**, because a constructor
  cannot await. Call `await client.connect()`, or use the client as an async
  context manager as above, when you want pricing events. HTTP calls work
  without it.
- **Closing is awaited**: `await client.close()`, or leave the `async with`.

Everything else - cache lookups and invalidation, request building, error
handling - is shared code rather than a second copy, so the two clients cannot
drift apart.

The cache providers remain synchronous. An in-memory lookup is not worth an
await; a Redis one is a short local round trip, so if you are caching in Redis
and want nothing at all blocking the loop, that is worth knowing.

## Configuration

You can connect using either convenience factory arguments or `SpaceConnectionOptions`.

### Option A: simple connection

```python
from space_client import SpaceClientFactory

client = SpaceClientFactory.connect("http://localhost:3000", "your-api-key")
```

### Option B: custom timeout

```python
client = SpaceClientFactory.connect("http://localhost:3000", "your-api-key", 15000)
```

### Option C: full options object

```python
from space_client import SpaceClientFactory
from space_client.types import CacheOptions, SpaceConnectionOptions

options = SpaceConnectionOptions(
	url="http://localhost:3000",
	api_key="your-api-key",
	timeout=15000,
	cache=CacheOptions(enabled=True),
)

client = SpaceClientFactory.connect(options)
```

`SpaceClientFactory.connect(...)` validation rules:

- URL is required and must start with `http://` or `https://`.
- API key is required.
- Timeout must be a positive number.

## API Overview

### SpaceClientFactory

| Method | Description |
|---|---|
| `connect(options)` | Creates a client from full options and validates required inputs. |
| `connect(url, api_key)` | Convenience overload with default timeout (`5000`). |
| `connect(url, api_key, timeout)` | Convenience overload with custom timeout. |
| `connect_async(...)` | Same arguments and same validation, returning an `AsyncSpaceClient`. |

### SpaceClient

Main entry point. Exposes modules as public fields:

- `contracts`
- `features`
- `pricings`
- `services`
- `cache`

Core methods:

| Method | Returns | Details |
|---|---|---|
| `is_connected_to_space()` | `bool` | HTTP health check against `/healthcheck`. |
| `on(event, callback)` | `None` | Registers event callback for supported event names. |
| `remove_listener(event)` | `None` | Removes one callback by event name. |
| `remove_all_listeners()` | `None` | Removes all event callbacks. |
| `connect()` | `None` | Connects/reconnects WebSocket namespace if disconnected. |
| `disconnect()` | `None` | Disconnects WebSocket namespace. |
| `close()` | `None` | Closes sockets, cache provider, and HTTP resources. |

Java-style aliases are also available (`isConnectedToSpace`, `removeListener`, `removeAllListeners`, etc.).

### AsyncSpaceClient

The same modules and the same methods, awaited. `is_connected_to_space()`,
`connect()`, `disconnect()` and `close()` are coroutines; `on`,
`remove_listener` and `remove_all_listeners` are not, since they only touch a
local registry. Supports `async with`.

### ContractModule

| Method | Returns |
|---|---|
| `get_contract(user_id)` | `Contract | None` |
| `add_contract(contract_to_create)` | `Contract | None` |
| `update_contract_subscription(user_id, new_subscription, expected_contract_version=None, keep_billing_period=False)` | `Contract | None` |
| `update_contract_subscription_by_group_id(group_id, new_subscription)` | `list[Contract] | None` |
| `update_contract_usage_levels(user_id, service_name, usage_levels_novations)` | `Contract | None` |
| `remove_contract(user_id)` | `None` |
| `get_pricings(user_id)` | `ContractPricings` |
| `preview_novation(user_id, new_subscription, keep_billing_period=False)` | `NovationPreview` |

Java-style aliases are also available (`getContract`, `addContract`, etc.).

### PricingModule

| Method | Returns |
|---|---|
| `get_yaml(service_name, pricing_version, etag=None)` | `PricingYaml` |

### ServiceModule

| Method | Returns |
|---|---|
| `get_new_contracts_pricing(service_name)` | `NewContractsPricing` |

### Pricing pages

A host backend renders a pricing page with the version a visitor would get, or
with the version a signed-in customer is on:

```python
# Signed-out visitor: the version SPACE assigns to new contracts.
current = client.services.get_new_contracts_pricing("acme")
pricing = client.pricings.get_yaml("acme", current.version)
print(current.source)  # "sphere-policy" | "default" | "latest-active"

# Signed-in customer: the version of THEIR contract.
mine = client.contracts.get_pricings("user-123")
acme = mine.services["acme"]
print(acme.version, acme.status, acme.plan, acme.add_ons, acme.new_contracts_version)
pricing = client.pricings.get_yaml("acme", acme.version)

# The file is returned byte for byte; keep its ETag to revalidate cheaply.
again = client.pricings.get_yaml("acme", acme.version, etag=pricing.etag)
if again.not_modified:
	...  # reuse pricing.yaml
```

### Changing a subscription safely

Preview first, then apply with the `contract_version` the preview was computed
on. If anything changed the contract in between, Space refuses with 412 and the
client raises `ContractChangedError` instead of overwriting it:

```python
from space_client import ContractChangedError
from space_client.types import NovationErrorCode, Subscription

change = Subscription(
	subscription_plans={"acme": "growth"},
	subscription_add_ons={"acme": {"extraStorage": 2}},
)  # contracted_services left empty: keep the versions or let the policy decide

preview = client.contracts.preview_novation("user-123", change, keep_billing_period=True)
if not preview.valid:
	for error in preview.errors:
		if error.code == NovationErrorCode.ADDON_NOT_AVAILABLE:
			...
		print(error.code, error.service, error.path, error.message)
else:
	print(preview.effective, preview.version_changes, preview.usage_warnings)
	try:
		client.contracts.update_contract_subscription(
			"user-123",
			preview.effective,
			expected_contract_version=preview.contract_version,
			keep_billing_period=True,
		)
	except ContractChangedError as changed:
		print("Changed meanwhile; current version is", changed.contract_version)
```

- `preview_novation` writes nothing and leaves the cache alone. An invalid
  change is an answer (`valid=False`, `errors`), not an exception. Unknown error
  codes are kept as strings.
- `expected_contract_version` is sent as `If-Match`. Without it the update is
  unconditional, exactly as before.
- `keep_billing_period=True` sends `?keepBillingPeriod=true`: history is still
  recorded but the billing period is not restarted.
- `Contract.contract_version` is filled from the body or, failing that, from the
  `ETag` of the update response. It is `None` on Space versions without it.

`get_yaml` raises `PricingYamlUnavailableError` (a `SpaceApiError`) when Space
knows the version but no longer has its original file. `get_pricings` is never
served from cache: its `contract_version` matters only while it is current.

> **Older Space versions.** These calls need a Space with the subscription-change
> API. Without it, `get_pricings` and `get_new_contracts_pricing` get Space's
> 404 (`SpaceApiError`). An older Space ignores `?format=yaml` and answers with
> the JSON pricing; `get_yaml` then raises `SpaceFeatureUnsupportedError`
> (`capability == "pricing-yaml"`) rather than returning JSON as if it were the
> file: only `application/yaml`, `text/yaml` and their `x-` variants are accepted.

### FeatureModule

| Method | Returns |
|---|---|
| `evaluate(user_id, feature_id, expected_consumption=None, details=False, server=False)` | `FeatureEvaluationResult` |
| `revert_evaluation(user_id, feature_id, revert_to_latest=True)` | `bool` |
| `generate_user_pricing_token(user_id)` | `str | None` |

Java-style aliases are also available (`revertEvaluation`, `generateUserPricingToken`).

## Data Models

Main model classes are exported from `space_client.types`:

- `SpaceConnectionOptions`
- `CacheOptions`, `CacheType`, `ExternalCacheConfig`, `RedisConfig`
- `Contract`, `ContractToCreate`, `Subscription`
- `PricingYaml`, `ContractPricings`, `ContractServicePricing`, `NewContractsPricing`
- `NovationPreview`, `NovationError`, `NovationErrorCode`, `NovationVersionChange`,
  `NovationUsageWarning`, `NovationBillingPeriod`
- `FeatureEvaluationResult`, `EvaluationError`
- `BillingPeriod`, `BillingPeriodToCreate`, `ContractHistoryEntry`, `UsageLevel`, `UserContact`
- `SpaceEvent`

## Caching

Built-in cache is enabled with:

```python
from space_client.types import CacheOptions, SpaceConnectionOptions

options = SpaceConnectionOptions(
	url="http://localhost:3000",
	api_key="your-api-key",
	cache=CacheOptions(enabled=True),
)
```

Redis cache example:

```python
from space_client.types import (
	CacheOptions,
	CacheType,
	ExternalCacheConfig,
	RedisConfig,
	SpaceConnectionOptions,
)

options = SpaceConnectionOptions(
	url="http://localhost:3000",
	api_key="your-api-key",
	cache=CacheOptions(
		enabled=True,
		type=CacheType.REDIS,
		external=ExternalCacheConfig(
			redis=RedisConfig(host="localhost", port=6379),
		),
		ttl=300,
	),
)
```

## WebSocket Events

Supported event names:

- `synchronized`
- `pricing_created`
- `pricing_archived`
- `pricing_actived`
- `service_disabled`
- `error`

Example:

```python
def on_pricing_created(details):
	print("Pricing changed:", details)


client.on("pricing_created", on_pricing_created)
client.connect()
```

## Error Handling

- Factory validation raises `ValueError` for invalid input.
- A request Space refused raises `SpaceApiError`, carrying the status code, the
  method and path, and whatever body came back.
- A request that never arrived raises `SpaceConnectionError`.
- `pricings.get_yaml(...)` raises `PricingYamlUnavailableError`, a subclass of
  `SpaceApiError`, when Space cannot provide the original Pricing2Yaml file.
- `SpaceFeatureUnsupportedError` (a `SpaceError`, not a `SpaceApiError`) means
  the connected Space lacks a feature the call depends on; `capability` names
  it with its `X-Space-Capabilities` token (e.g. `pricing-yaml` when `get_yaml`
  receives JSON from a Space that ignores `format=yaml`).
- `contracts.update_contract_subscription(..., expected_contract_version=...)`
  raises `ContractChangedError`, a subclass of `SpaceApiError`, when the
  contract changed since that version (HTTP 412). It carries `user_id`,
  `expected_contract_version` and the current `contract_version`, and the
  user's cached entries are invalidated.
- `FeatureModule.evaluate(...)` returns an object containing `error` when Space
  answers successfully with something that is not a usable evaluation.

Both clients raise the same exceptions.

## Testing

Run tests locally:

```bash
pip install -e .[dev]
pytest
```

Tests never reach a live Space: the older ones stub the HTTP client, the
subscription-change ones mock httpx's transport with
[respx](https://lundberg.github.io/respx/) and run against both the sync and the
async client.

CI executes tests on every pull request for Python `3.10`, `3.11`, and `3.12`.

## Contributing

1. Create a branch from `main`.
2. Add or update tests for every behavior change.
3. Open a pull request.

## License

MIT License.
