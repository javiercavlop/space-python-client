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

    def __init__(self, status_code: int, method: str, path: str, payload: Any = None):
        self.status_code = status_code
        self.method = method
        self.path = path
        self.payload = payload

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
