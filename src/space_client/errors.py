"""Errors the client raises when Space refuses or cannot be reached.

Every HTTP call used to answer ``None`` for three different situations: the
server refused the request, the server could not be reached, and the server
answered with nothing. A caller holding a ``None`` could not tell which had
happened, so the only two options were to treat every failure as absence or to
guess.

That matters most for the calls that change something. ``add_contract``
returning ``None`` looks exactly the same whether a contract was created and
the body was empty or whether Space answered ``400 Plan FREE for service X not
found in the request organization`` - and in the second case an application
carries on believing its user is subscribed.

These are deliberately narrow: ``SpaceApiError`` carries what the server said,
``SpaceConnectionError`` says it was never reached, and both derive from
``SpaceError`` so a caller who does not care about the difference can catch one
thing.
"""

from __future__ import annotations

from typing import Any


class SpaceError(Exception):
    """Anything that went wrong talking to Space."""


class SpaceConnectionError(SpaceError):
    """Space could not be reached at all.

    A timeout, a refused connection, DNS - the request never got an answer, so
    nothing can be said about whether it took effect.
    """


class SpaceApiError(SpaceError):
    """Space answered, and the answer was a refusal.

    Carries the status and the body, because the body is where Space explains
    itself: "Plan FREE for service openbinding not found in the request
    organization" is the difference between a bug and a configuration mistake,
    and it is lost the moment the response is turned into ``None``.
    """

    def __init__(
        self,
        status_code: int,
        method: str,
        path: str,
        payload: Any = None,
        headers: Any = None,
    ):
        self.status_code = status_code
        self.method = method
        self.path = path
        self.payload = payload
        # Response headers, names in lower case (``retry-after``...).
        self.headers: dict[str, str] = {
            str(name).lower(): str(value) for name, value in dict(headers or {}).items()
        }

        detail = _describe(payload)
        super().__init__(
            f"Space answered {status_code} to {method} {path}"
            + (f": {detail}" if detail else "")
        )


def _describe(payload: Any) -> str:
    """The server's own explanation, when it gave one.

    Space reports errors as ``{"error": "..."}`` and sometimes adds
    ``details``; anything else is included as-is rather than dropped, since an
    unexpected shape is still more informative than nothing.
    """
    if payload is None:
        return ""
    if isinstance(payload, dict):
        message = payload.get("error") or payload.get("message")
        details = payload.get("details")
        if message and details:
            return f"{message} ({details})"
        if message:
            return str(message)
    text = str(payload).strip()
    return text[:500]


class SpaceFeatureUnsupportedError(SpaceError):
    """The connected Space does not implement a feature the call depends on.

    Raised instead of quietly doing something else: returning the JSON pricing
    as if it were the YAML file, or writing a novation without the guarantee
    the caller asked for. ``capability`` is the ``X-Space-Capabilities`` token
    of the missing feature (``pricing-yaml``, ``contract-version``,
    ``keep-billing-period``...).

    ``written`` says whether Space changed anything. It is False when the
    client refused before sending a write (or for a read); it is True only for
    the post-write backstop, when the response shows that Space applied a
    novation without the guarantee - the change is then in place and
    ``contract`` holds what Space answered.

    Not a :class:`SpaceApiError`: Space did not refuse anything.
    """

    code = "SPACE_FEATURE_UNSUPPORTED"

    def __init__(
        self,
        capability: str,
        message: str | None = None,
        written: bool = False,
        contract: Any = None,
    ):
        self.capability = capability
        self.written = written
        self.contract = contract
        super().__init__(message or f'The connected Space does not support "{capability}"')


class PricingYamlUnavailableError(SpaceApiError):
    """Space knows the pricing version but cannot hand out its original file.

    Raised by ``pricings.get_yaml`` on ``404 {"error": "PRICING_YAML_UNAVAILABLE"}``:
    historical SPHERE-linked versions may survive only as parsed JSON. It is a
    :class:`SpaceApiError`, so code that already catches refusals still does.
    """

    def __init__(
        self,
        status_code: int,
        method: str,
        path: str,
        payload: Any = None,
        service_name: str = "",
        pricing_version: str = "",
    ):
        super().__init__(status_code, method, path, payload)
        self.service_name = service_name
        self.pricing_version = pricing_version

    @classmethod
    def from_error(
        cls, error: SpaceApiError, service_name: str, pricing_version: str
    ) -> "PricingYamlUnavailableError":
        return cls(
            error.status_code,
            error.method,
            error.path,
            error.payload,
            service_name=service_name,
            pricing_version=pricing_version,
        )


class ContractChangedError(SpaceApiError):
    """The contract changed since the version the caller computed its change on.

    Raised by ``contracts.update_contract_subscription`` when an
    ``expected_contract_version`` was given and Space answered
    ``412 {"error": "CONTRACT_CHANGED", "contractVersion": "<current>"}``:
    someone else novated, renewed or edited the contract in the meantime.
    Re-read it (or preview again), show the customer the new situation and ask
    them to confirm; ``contract_version`` is the version to send next time.
    """

    code = "CONTRACT_CHANGED"

    def __init__(
        self,
        status_code: int,
        method: str,
        path: str,
        payload: Any = None,
        user_id: str = "",
        expected_contract_version: str | None = None,
        contract_version: str | None = None,
    ):
        super().__init__(status_code, method, path, payload)
        self.user_id = user_id
        self.expected_contract_version = expected_contract_version
        self.contract_version = contract_version
        message = (
            f"The contract of user {user_id} changed (expected "
            f"{expected_contract_version or 'unknown'}, current {contract_version or 'unknown'})"
        )
        self.args = (message,)

    @classmethod
    def from_error(
        cls, error: SpaceApiError, user_id: str, expected_contract_version: str | None
    ) -> "ContractChangedError":
        payload = error.payload if isinstance(error.payload, dict) else {}
        return cls(
            error.status_code,
            error.method,
            error.path,
            error.payload,
            user_id=user_id,
            expected_contract_version=expected_contract_version,
            contract_version=payload.get("contractVersion"),
        )


class IdempotencyError(SpaceApiError):
    """Space refused a write because of its ``Idempotency-Key``.

    Raised for the three refusals of the ``idempotency-key`` capability; this
    request applied nothing. ``code`` is Space's code and ``idempotency_key``
    the key the request carried. A :class:`SpaceApiError`, so code that
    catches refusals still does.
    """

    code = ""

    def __init__(
        self,
        status_code: int,
        method: str,
        path: str,
        payload: Any = None,
        headers: Any = None,
        idempotency_key: str = "",
    ):
        super().__init__(status_code, method, path, payload, headers)
        self.idempotency_key = idempotency_key

    @classmethod
    def from_error(cls, error: SpaceApiError, idempotency_key: str) -> "IdempotencyError":
        return cls(
            error.status_code,
            error.method,
            error.path,
            error.payload,
            error.headers,
            idempotency_key=idempotency_key,
        )


class IdempotencyConflictError(IdempotencyError):
    """``409 IDEMPOTENCY_CONFLICT``: the key was used for a different request.

    Another body, query, route or ``If-Match`` was sent with this key before.
    Nothing was applied. A new operation needs a new key; reusing one for
    another operation is a bug of the caller.
    """

    code = "IDEMPOTENCY_CONFLICT"


class IdempotencyInProgressError(IdempotencyError):
    """``409 IDEMPOTENCY_IN_PROGRESS``: a request with the key is still running.

    Raised once the client's retries (if any) are spent. Retry later with the
    SAME key: Space then replays the outcome of the running request.
    ``retry_after`` is Space's ``Retry-After`` in seconds, or None.
    """

    code = "IDEMPOTENCY_IN_PROGRESS"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.retry_after: int | None = parse_retry_after(self.headers.get("retry-after"))


class IdempotencyKeyInvalidError(IdempotencyError):
    """``400 IDEMPOTENCY_KEY_INVALID``: the key is empty or longer than 255 characters."""

    code = "IDEMPOTENCY_KEY_INVALID"


def parse_retry_after(value: str | None) -> int | None:
    """``Retry-After`` in seconds: delta-seconds or an HTTP date; None if absent or unreadable."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    if text.isdigit():
        return int(text)
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime

    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    seconds = (moment - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(seconds + 0.999))
