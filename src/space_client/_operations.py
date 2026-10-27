"""What each call does, with the transport left out.

The synchronous and asynchronous clients differ in exactly one respect: how a
request is sent and awaited. Everything else - which cache key to look under,
what to send, how to read the answer back, which cache entries the answer
invalidates - is the same in both, and lives here so it is written once.

An :class:`Operation` is a description, not a call. It says what to look for in
the cache before asking Space, what request to make, and what to do with the
body that comes back. The runners in :mod:`space_client.modules` supply the one
part that must differ.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Generator, Generic, TypeVar, Union
from urllib.parse import quote

from .errors import (
    ContractChangedError,
    PricingYamlUnavailableError,
    SpaceApiError,
    SpaceFeatureUnsupportedError,
)
from .types.models import (
    Contract,
    ContractPricings,
    ContractToCreate,
    EvaluationError,
    FeatureEvaluationResult,
    NewContractsPricing,
    NovationPreview,
    PricingYaml,
    Subscription,
)

if TYPE_CHECKING:
    from .modules.cache_module import CacheModule

T = TypeVar("T")


@dataclass(frozen=True)
class Operation(Generic[T]):
    """One call to Space, described rather than made.

    Attributes:
        method: HTTP method.
        path: Path below ``/api/v1``.
        json: Body to send, if any.
        no_content: True when the call answers with a status rather than a body,
            in which case ``parse`` receives the boolean from the transport.
        cache_hit: Consulted before the request; a non-None result is returned
            to the caller and the request is never made. Only set for calls that
            are safe to serve from cache.
        parse: Turns what came back into the caller's result, and applies
            whatever the answer implies for the cache.
        headers: Extra request headers (``If-None-Match``, ``If-Match``...).
        raw: True when the answer is more than a JSON body - a YAML file, an
            ``ETag`` header, a 304 - in which case ``parse`` receives the HTTP
            response itself instead of the decoded body.
        accept: Statuses outside 2xx that are answers rather than refusals
            (``304 Not Modified``). Only meaningful with ``raw``.
        on_refusal: Called with the :class:`SpaceApiError` when Space refuses;
            it may raise a more specific error (which must still be a
            ``SpaceApiError``) or return to let the generic one propagate.
    """

    method: str
    path: str
    json: Any | None = None
    no_content: bool = False
    cache_hit: Callable[["CacheModule"], T | None] | None = None
    parse: Callable[["CacheModule", Any], T] = field(
        default=lambda _cache, payload: payload  # type: ignore[assignment,return-value]
    )
    headers: dict[str, str] | None = None
    raw: bool = False
    accept: frozenset[int] = frozenset()
    on_refusal: Callable[["CacheModule", SpaceApiError], None] | None = None


Flow = Generator[Operation[Any], Any, T]
"""Several requests that make one call: yields each operation, receives its
result, returns the call's result. Used when what to ask next depends on the
previous answer (a capability check before a write). The runners drive it, so
it too is written once for both clients."""

Work = Union[Operation[T], Flow[T]]

Capabilities = Callable[[], "frozenset[str] | None"]
"""The ``X-Space-Capabilities`` tokens this client saw recently, or None when
it has to ask again (see ``BaseSpaceClient.CAPABILITIES_TTL_SECONDS``)."""


def _no_capabilities() -> "frozenset[str] | None":
    return None


# S0 tokens the client depends on.
PRICING_YAML = "pricing-yaml"
CONTRACT_VERSION = "contract-version"
KEEP_BILLING_PERIOD = "keep-billing-period"

_OPTION_OF = {
    CONTRACT_VERSION: "expected_contract_version (If-Match)",
    KEEP_BILLING_PERIOD: "keep_billing_period",
}


class _Unset:
    """Default of ``expected_contract_version``: told apart from an explicit None."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()


def _segment(value: str) -> str:
    """One path segment, escaped: a version like ``v1/2`` must stay one segment."""
    return quote(str(value), safe="")


def _quote_etag(etag: str) -> str:
    """Entity tags travel quoted; accept them from callers either way."""
    if re.fullmatch(r'(W/)?".*"', etag):
        return etag
    return f'"{etag}"'


def _unquote_etag(etag: str) -> str:
    return re.sub(r'^(W/)?"(.*)"$', r"\2", etag)


def _response_headers(response: Any) -> Any:
    return getattr(response, "headers", None) or {}


def _decode(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _error_code(payload: Any) -> str | None:
    return payload.get("error") if isinstance(payload, dict) else None


# --------------------------------------------------------------------------
# Contracts
# --------------------------------------------------------------------------


def get_contract(user_id: str) -> Operation[Contract | None]:
    def hit(cache: "CacheModule") -> Contract | None:
        return cache.get(cache.get_contract_key(user_id), parser=Contract.from_dict)

    def parse(cache: "CacheModule", payload: Any) -> Contract | None:
        if payload is None:
            return None
        contract = Contract.from_dict(payload)
        if cache.is_enabled():
            cache.set(cache.get_contract_key(user_id), payload)
        return contract

    return Operation("GET", f"/contracts/{user_id}", cache_hit=hit, parse=parse)


def add_contract(contract_to_create: ContractToCreate) -> Operation[Contract | None]:
    def parse(cache: "CacheModule", payload: Any) -> Contract | None:
        if payload is None:
            return None
        contract = Contract.from_dict(payload)
        if cache.is_enabled() and contract.user_id is not None:
            cache.invalidate_user(contract.user_id)
            cache.set(cache.get_contract_key(contract.user_id), payload)
        return contract

    return Operation("POST", "/contracts", json=contract_to_create.to_dict(), parse=parse)


def update_contract_subscription(
    user_id: str,
    new_subscription: Subscription,
    expected_contract_version: Any = UNSET,
    keep_billing_period: bool = False,
    capabilities: Capabilities = _no_capabilities,
) -> Work[Contract | None]:
    """The novation, guarded when the caller asked for a guarantee (S0).

    Without options it is the single PUT it always was. With
    ``expected_contract_version`` or ``keep_billing_period`` the client first
    makes sure Space advertises ``contract-version`` / ``keep-billing-period``
    (reading the contract once when it does not know), refuses without writing
    if it does not, and checks the answer for signs the option was ignored.
    """
    conditional = expected_contract_version is not UNSET
    if conditional and (not isinstance(expected_contract_version, str) or not expected_contract_version):
        # None here is almost always a version that was never known (a
        # preview from an old Space): sending no If-Match would turn a
        # conditional update into an unconditional one. Raised from the flow,
        # so the async client raises it when awaited, like any other error.
        return _refuse_empty_version()
    expected: str | None = expected_contract_version if conditional else None

    required = [
        capability
        for capability, wanted in ((CONTRACT_VERSION, conditional), (KEEP_BILLING_PERIOD, keep_billing_period))
        if wanted
    ]
    if not required:
        return _novation_put(user_id, new_subscription, None, False)
    return _guarded_novation(user_id, new_subscription, expected, keep_billing_period, required, capabilities)


def _refuse_empty_version() -> Flow[Contract | None]:
    raise ValueError(
        "expected_contract_version must be a non-empty contract version; "
        "leave it out for an unconditional update"
    )
    yield  # pragma: no cover - makes this a generator


def _guarded_novation(
    user_id: str,
    new_subscription: Subscription,
    expected: str | None,
    keep_billing_period: bool,
    required: list[str],
    capabilities: Capabilities,
) -> Flow[Contract | None]:
    before: Contract | None = None
    known = capabilities()
    if known is None:
        # The answer carries the capabilities, and its ETag lets a stale
        # expected version fail before anything is written.
        before = yield _novation_probe(user_id, expected, required, capabilities)
    else:
        _require(required, known)
    return (
        yield _novation_put(user_id, new_subscription, expected, keep_billing_period, before)
    )


def _require(required: list[str], known: "frozenset[str]") -> None:
    for capability in required:
        if capability not in known:
            raise SpaceFeatureUnsupportedError(
                capability,
                f"The connected Space does not support {_OPTION_OF[capability]}: nothing was written",
            )


def _novation_probe(
    user_id: str, expected: str | None, required: list[str], capabilities: Capabilities
) -> Operation[Contract]:
    path = f"/contracts/{user_id}"

    def parse(cache: "CacheModule", response: Any) -> Contract:
        _require(required, capabilities() or frozenset())
        payload = _decode(response)
        payload = payload if isinstance(payload, dict) else {}
        etag = _response_headers(response).get("etag")
        current = _unquote_etag(etag) if etag else payload.get("contractVersion")
        if expected is not None and current is not None and current != _unquote_etag(expected):
            # Early and not atomic; If-Match on the PUT stays the real check.
            if cache.is_enabled():
                cache.invalidate_user(user_id)
            raise ContractChangedError(
                412,
                "GET",
                path,
                {"error": ContractChangedError.code, "contractVersion": current},
                user_id=user_id,
                expected_contract_version=expected,
                contract_version=current,
            )
        return Contract.from_dict(payload)

    return Operation("GET", path, raw=True, parse=parse)


def _novation_put(
    user_id: str,
    new_subscription: Subscription,
    expected: str | None,
    keep_billing_period: bool,
    before: Contract | None = None,
) -> Operation[Contract | None]:
    headers: dict[str, str] | None = None
    if expected is not None:
        headers = {"If-Match": _quote_etag(expected)}

    replace = _replace_one(user_id)

    def parse(cache: "CacheModule", response: Any) -> Contract | None:
        payload = _decode(response)
        # Servers that send the version only as an ETag still fill it in.
        etag = _response_headers(response).get("etag")
        if isinstance(payload, dict) and payload.get("contractVersion") is None and etag:
            payload = {**payload, "contractVersion": _unquote_etag(etag)}
        if expected is not None or keep_billing_period:
            ignored = _ignored_option(expected, keep_billing_period, etag, payload, before)
            if ignored is not None:
                # Space wrote the novation without the guarantee. Say so, and
                # keep nothing cached for a contract in a state nobody chose.
                if cache.is_enabled():
                    cache.invalidate_user(user_id)
                capability, message = ignored
                raise SpaceFeatureUnsupportedError(
                    capability,
                    message,
                    written=True,
                    contract=Contract.from_dict(payload) if isinstance(payload, dict) else None,
                )
        return replace(cache, payload)

    def on_refusal(cache: "CacheModule", error: SpaceApiError) -> None:
        if error.status_code == 412:
            # Whatever we hold for this user is known to be stale.
            if cache.is_enabled():
                cache.invalidate_user(user_id)
            raise ContractChangedError.from_error(error, user_id, expected)

    # The path is left unescaped, as it always was for this call, so that
    # callers relying on the old URL see no change.
    return Operation(
        "PUT",
        f"/contracts/{user_id}{_novation_query(keep_billing_period)}",
        json=new_subscription.to_dict(),
        headers=headers,
        raw=True,
        parse=parse,
        on_refusal=on_refusal,
    )


def _ignored_option(
    expected: str | None,
    keep_billing_period: bool,
    etag: str | None,
    payload: Any,
    before: Contract | None,
) -> tuple[str, str] | None:
    """Post-write backstop: does the stored contract show an option was ignored?"""
    if expected is not None and not etag:
        return (
            CONTRACT_VERSION,
            "Space applied the novation without an ETag: If-Match (expected_contract_version) "
            "was not enforced",
        )
    if keep_billing_period and isinstance(payload, dict):
        after = Contract.from_dict(payload)
        previous = before.billing_period.start_date if before and before.billing_period else None
        if previous is None and after.history:
            # Space records the replaced period in the history entry it pushes.
            previous = after.history[-1].start_date
        current = after.billing_period.start_date if after.billing_period else None
        if previous is not None and current is not None and not _same_instant(previous, current):
            return (
                KEEP_BILLING_PERIOD,
                "Space applied the novation but restarted the billing period "
                "(keep_billing_period was ignored)",
            )
    return None


def _same_instant(a: Any, b: Any) -> bool:
    def parse(value: Any) -> Any:
        if isinstance(value, datetime):
            moment = value
        else:
            try:
                moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return str(value)
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)

    first, second = parse(a), parse(b)
    if isinstance(first, datetime) and isinstance(second, datetime):
        return first == second
    return str(a) == str(b)


def preview_novation(
    user_id: str,
    new_subscription: Subscription,
    keep_billing_period: bool = False,
    capabilities: Capabilities = _no_capabilities,
) -> Operation[NovationPreview]:
    body = new_subscription.to_dict()
    if not body.get("contractedServices"):
        # Omitted means "keep the current versions or let the policy decide".
        body.pop("contractedServices", None)

    # A dry run writes nothing, so it leaves the cache alone.
    def parse(_cache: "CacheModule", payload: Any) -> NovationPreview:
        if keep_billing_period and KEEP_BILLING_PERIOD not in (capabilities() or frozenset()):
            # The preview would describe a restart the caller asked to avoid.
            raise SpaceFeatureUnsupportedError(
                KEEP_BILLING_PERIOD,
                "The connected Space does not support keep_billing_period: the preview ignored it",
            )
        return NovationPreview.from_dict(payload if isinstance(payload, dict) else {})

    return Operation(
        "POST",
        f"/contracts/{_segment(user_id)}/novation/preview{_novation_query(keep_billing_period)}",
        json=body,
        parse=parse,
    )


def _novation_query(keep_billing_period: bool) -> str:
    return "?keepBillingPeriod=true" if keep_billing_period else ""


def update_contract_subscription_by_group_id(
    group_id: str,
    new_subscription: Subscription,
) -> Operation[list[Contract] | None]:
    def parse(cache: "CacheModule", payload: Any) -> list[Contract] | None:
        if payload is None or not isinstance(payload, list):
            return None
        contracts = [Contract.from_dict(item) for item in payload]
        if cache.is_enabled():
            for contract, raw in zip(contracts, payload):
                if contract.user_id is None:
                    continue
                cache.invalidate_user(contract.user_id)
                cache.set(cache.get_contract_key(contract.user_id), raw)
        return contracts

    return Operation(
        "PUT",
        f"/contracts?groupId={group_id}",
        json=new_subscription.to_dict(),
        parse=parse,
    )


def update_contract_usage_levels(
    user_id: str,
    service_name: str,
    usage_levels_novations: dict[str, int | float],
) -> Operation[Contract | None]:
    return Operation(
        "PUT",
        f"/contracts/{user_id}/usageLevels",
        json={service_name: usage_levels_novations},
        parse=_replace_one(user_id),
    )


def remove_contract(user_id: str) -> Operation[None]:
    def parse(cache: "CacheModule", accepted: Any) -> None:
        if not accepted:
            return None
        if cache.is_enabled():
            cache.invalidate_user(user_id)
        return None

    return Operation("DELETE", f"/contracts/{user_id}", no_content=True, parse=parse)


def get_contract_pricings(user_id: str) -> Operation[ContractPricings]:
    # Never cached: the point of the answer is its contractVersion, which is
    # only useful while it is current.
    def parse(_cache: "CacheModule", payload: Any) -> ContractPricings:
        return ContractPricings.from_dict(payload if isinstance(payload, dict) else {})

    return Operation("GET", f"/contracts/{_segment(user_id)}/pricings", parse=parse)


def _replace_one(user_id: str) -> Callable[["CacheModule", Any], Contract | None]:
    """The answer is this user's whole contract: forget what we knew, keep it."""

    def parse(cache: "CacheModule", payload: Any) -> Contract | None:
        if payload is None:
            return None
        contract = Contract.from_dict(payload)
        if cache.is_enabled():
            cache.invalidate_user(user_id)
            cache.set(cache.get_contract_key(user_id), payload)
        return contract

    return parse


# --------------------------------------------------------------------------
# Pricings and services
# --------------------------------------------------------------------------


_YAML_MEDIA_TYPES = frozenset(
    {"application/yaml", "text/yaml", "application/x-yaml", "text/x-yaml"}
)


def _media_type(response: Any) -> str:
    content_type = response.headers.get("content-type") or ""
    return content_type.split(";", 1)[0].strip().lower()


def get_pricing_yaml(
    service_name: str, pricing_version: str, etag: str | None = None
) -> Operation[PricingYaml]:
    headers = {"Accept": "application/yaml"}
    if etag:
        headers["If-None-Match"] = _quote_etag(etag)

    def parse(_cache: "CacheModule", response: Any) -> PricingYaml:
        received_etag = response.headers.get("etag")
        syntax_version = response.headers.get("x-pricing-syntax-version")
        if response.status_code == 304:
            return PricingYaml(
                yaml=None,
                etag=received_etag or (_quote_etag(etag) if etag else None),
                syntax_version=syntax_version,
                not_modified=True,
            )
        media_type = _media_type(response)
        if media_type not in _YAML_MEDIA_TYPES:
            # Space without S2 ignores ?format=yaml and answers with the JSON
            # pricing: that is not the file, so do not hand it out as one.
            raise SpaceFeatureUnsupportedError(
                PRICING_YAML,
                f"Space answered {media_type or 'without a Content-Type'} for "
                f"{service_name}@{pricing_version}: this Space version does not support "
                "format=yaml",
            )
        return PricingYaml(
            yaml=response.text,
            etag=received_etag,
            syntax_version=syntax_version,
            not_modified=False,
        )

    def on_refusal(_cache: "CacheModule", error: SpaceApiError) -> None:
        if error.status_code == 404 and _error_code(error.payload) == "PRICING_YAML_UNAVAILABLE":
            raise PricingYamlUnavailableError.from_error(error, service_name, pricing_version)

    return Operation(
        "GET",
        f"/services/{_segment(service_name)}/pricings/{_segment(pricing_version)}?format=yaml",
        headers=headers,
        raw=True,
        accept=frozenset({304}),
        parse=parse,
        on_refusal=on_refusal,
    )


def get_new_contracts_pricing(service_name: str) -> Operation[NewContractsPricing]:
    def parse(_cache: "CacheModule", payload: Any) -> NewContractsPricing:
        return NewContractsPricing.from_dict(payload if isinstance(payload, dict) else {})

    return Operation(
        "GET", f"/services/{_segment(service_name)}/pricings/new-contracts", parse=parse
    )


# --------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------


def evaluate(
    user_id: str,
    feature_id: str,
    expected_consumption: dict[str, int | float] | None = None,
    details: bool = False,
    server: bool = False,
) -> Operation[FeatureEvaluationResult]:
    expected_consumption = expected_consumption or {}

    # An evaluation that spends nothing can be answered from cache; one that
    # spends something has to reach Space, and invalidates what we held.
    is_read_only = len(expected_consumption) == 0

    def hit(cache: "CacheModule") -> FeatureEvaluationResult | None:
        if not is_read_only:
            return None
        return cache.get(
            cache.get_feature_key(user_id, feature_id),
            parser=FeatureEvaluationResult.from_dict,
        )

    def parse(cache: "CacheModule", payload: Any) -> FeatureEvaluationResult:
        if payload is None:
            return FeatureEvaluationResult(
                eval=False,
                used={},
                limit={},
                error=EvaluationError(code="IO_ERROR", message="Error while evaluating feature"),
            )

        result = FeatureEvaluationResult.from_dict(payload)

        if cache.is_enabled():
            if is_read_only:
                cache.set(cache.get_feature_key(user_id, feature_id), payload, ttl=60)
            else:
                cache.delete(cache.get_feature_key(user_id, feature_id))
                cache.delete(cache.get_contract_key(user_id))
                cache.delete(cache.get_pricing_token_key(user_id))

        return result

    query_params: list[str] = []
    if details:
        query_params.append("details=true")
    if server:
        query_params.append("server=true")
    query = "?" + "&".join(query_params) if query_params else ""

    return Operation(
        "POST",
        f"/features/{user_id}/{feature_id}{query}",
        json=expected_consumption,
        cache_hit=hit,
        parse=parse,
    )


def revert_evaluation(user_id: str, feature_id: str, revert_to_latest: bool = True) -> Operation[bool]:
    def parse(cache: "CacheModule", accepted: Any) -> bool:
        if not accepted:
            return False
        if cache.is_enabled():
            cache.delete(cache.get_feature_key(user_id, feature_id))
            cache.delete(cache.get_contract_key(user_id))
            cache.delete(cache.get_pricing_token_key(user_id))
        return True

    return Operation(
        "POST",
        f"/features/{user_id}?revert=true&latest={str(revert_to_latest).lower()}",
        json={},
        no_content=True,
        parse=parse,
    )


def generate_user_pricing_token(user_id: str) -> Operation[str | None]:
    def hit(cache: "CacheModule") -> str | None:
        cached = cache.get(cache.get_pricing_token_key(user_id))
        return cached if isinstance(cached, str) else None

    def parse(cache: "CacheModule", payload: Any) -> str | None:
        if payload is None:
            return None
        token = payload.get("pricingToken") if isinstance(payload, dict) else None
        if cache.is_enabled() and token is not None:
            cache.set(cache.get_pricing_token_key(user_id), token, ttl=900)
        return token

    return Operation(
        "POST",
        f"/features/{user_id}/pricing-token",
        json={},
        cache_hit=hit,
        parse=parse,
    )


def healthcheck() -> Operation[bool]:
    def parse(_cache: "CacheModule", payload: Any) -> bool:
        return isinstance(payload, dict) and "message" in payload

    return Operation("GET", "/healthcheck", parse=parse)
