from __future__ import annotations

from typing import Any

from .. import _operations as operations
from .._result import Result
from ..types.models import (
    Contract,
    ContractPricings,
    ContractToCreate,
    NovationPreview,
    Subscription,
)
from ._runner import AsyncRunner, SyncRunner


class _ContractCalls:
    """The contract calls, written once.

    Each method says what to do and hands it to ``self._run``, which is the only
    part that differs between the synchronous and asynchronous clients. On
    :class:`ContractModule` these methods return their value; on
    :class:`AsyncContractModule` they return an awaitable of that value.
    """

    _run: Any
    _capabilities: Any

    def get_contract(self, user_id: str) -> Result[Contract | None]:
        """Get a contract by user identifier.

        Args:
            user_id (str): User identifier.

        Returns:
            Contract | None: Parsed contract on success, otherwise None.
        """
        return self._run(operations.get_contract(user_id))

    def add_contract(
        self, contract_to_create: ContractToCreate, idempotency_key: str | None = None
    ) -> Result[Contract | None]:
        """Create a contract in Space.

        Sent with an ``Idempotency-Key``: on a Space with ``idempotency-key``, a
        retry of a creation that succeeded answers the created contract instead
        of ``409``.

        Args:
            contract_to_create (ContractToCreate): Contract creation payload.
            idempotency_key (str | None): ``Idempotency-Key`` of this
                operation (1 to 255 characters). Default: a new UUID v4, reused
                on the client's own retries. Pass a key stored with the
                operation to make retries after a restart safe; Space must then
                advertise ``idempotency-key`` (``SpaceFeatureUnsupportedError``
                otherwise, nothing sent).

        Returns:
            Contract | None: Created contract on success, otherwise None.
        """
        return self._run(operations.add_contract(contract_to_create, idempotency_key))

    def update_contract_subscription(
        self,
        user_id: str,
        new_subscription: Subscription,
        expected_contract_version: str = operations.UNSET,
        keep_billing_period: bool = False,
        reject_private: bool = False,
        idempotency_key: str | None = None,
    ) -> Result[Contract | None]:
        """Update subscription for a single user (novation).

        Args:
            user_id (str): User identifier.
            new_subscription (Subscription): Subscription update payload.
            expected_contract_version (str): The ``contract_version`` the
                change was computed on (from ``get_contract``, ``get_pricings`` or
                ``preview_novation``). Sent as ``If-Match``; if the contract
                changed meanwhile Space answers 412 and this raises
                :class:`~space_client.errors.ContractChangedError`. Leave it out
                for an unconditional update, as before; an explicit ``None`` or
                ``""`` raises ``ValueError`` instead of silently dropping the
                condition.
            keep_billing_period (bool): Keep the current billing period instead of
                restarting it (``?keepBillingPeriod=true``). Default: restart.
            reject_private (bool): Ask Space to reject private plans and add-ons
                (``?rejectPrivate=true``). Default: Space accepts them, as before.
            idempotency_key (str | None): ``Idempotency-Key`` of this
                operation (1 to 255 characters). Default: a new UUID v4, reused
                on the client's own retries. Pass a key stored with the
                operation to make retries after a restart safe; Space must then
                advertise ``idempotency-key`` (``SpaceFeatureUnsupportedError``
                otherwise, nothing sent).

        The options need a Space that implements them (``X-Space-Capabilities``
        tokens ``contract-version``, ``keep-billing-period``, ``reject-private``). Before writing,
        the client checks them - from any recent response, or by reading the
        contract once, which also lets a stale expected version fail early - and
        after writing it checks the answer (an ``ETag``; an unchanged billing
        period start). Without options the request is the single PUT it always
        was.

        Returns:
            Contract | None: Updated contract on success, otherwise None.

        Raises:
            ValueError: ``expected_contract_version`` given but empty.
            ContractChangedError: ``expected_contract_version`` no longer matches.
            SpaceFeatureUnsupportedError: Space does not implement a requested
                option. ``written`` is False when nothing was sent, True when
                Space applied the change without the guarantee (``contract``
                then holds what it stored).
            SpaceApiError: Any other refusal.
        """
        return self._run(
            operations.update_contract_subscription(
                user_id,
                new_subscription,
                expected_contract_version,
                keep_billing_period,
                capabilities=self._capabilities,
                reject_private=reject_private,
                idempotency_key=idempotency_key,
            )
        )

    def preview_novation(
        self,
        user_id: str,
        new_subscription: Subscription,
        keep_billing_period: bool = False,
        reject_private: bool = False,
    ) -> Result[NovationPreview]:
        """Preview a novation without applying it.

        Calls ``POST /contracts/{userId}/novation/preview``. Space runs the same
        policy and validation as :meth:`update_contract_subscription` and writes
        nothing. An invalid change is reported as ``valid=False`` with
        ``errors``, not raised. Leave ``contracted_services`` empty to keep the
        current versions or let the SPHERE policy decide.

        Args:
            user_id (str): User identifier.
            new_subscription (Subscription): The candidate subscription.
            keep_billing_period (bool): Preview keeping the current billing period.
                Raises ``SpaceFeatureUnsupportedError`` if the Space that answered
                does not advertise ``keep-billing-period``.
            reject_private (bool): Preview with private plans and add-ons
                rejected (``?rejectPrivate=true``); raises the same error when
                Space does not advertise ``reject-private``.

        Returns:
            NovationPreview: Validity, errors, the effective subscription,
            version changes, usage warnings, the resulting billing period and
            the current ``contract_version``.
        """
        return self._run(
            operations.preview_novation(
                user_id,
                new_subscription,
                keep_billing_period,
                capabilities=self._capabilities,
                reject_private=reject_private,
            )
        )

    def update_contract_subscription_by_group_id(
        self,
        group_id: str,
        new_subscription: Subscription,
        idempotency_key: str | None = None,
    ) -> Result[list[Contract] | None]:
        """Update subscription for every contract in a group.

        Args:
            group_id (str): Group identifier.
            new_subscription (Subscription): Subscription update payload.
            idempotency_key (str | None): ``Idempotency-Key`` of this
                operation (1 to 255 characters). Default: a new UUID v4, reused
                on the client's own retries. Pass a key stored with the
                operation to make retries after a restart safe; Space must then
                advertise ``idempotency-key`` (``SpaceFeatureUnsupportedError``
                otherwise, nothing sent).

        Returns:
            list[Contract] | None: Updated contracts list on success, otherwise None.
        """
        return self._run(
            operations.update_contract_subscription_by_group_id(
                group_id, new_subscription, idempotency_key
            )
        )

    def update_contract_usage_levels(
        self,
        user_id: str,
        service_name: str,
        usage_levels_novations: dict[str, int | float],
        idempotency_key: str | None = None,
    ) -> Result[Contract | None]:
        """Update usage levels for one user service.

        Args:
            user_id (str): User identifier.
            service_name (str): Service name used as payload root key.
            usage_levels_novations (dict[str, int | float]): Feature usage deltas by key.
            idempotency_key (str | None): ``Idempotency-Key`` of this
                operation (1 to 255 characters). Default: a new UUID v4, reused
                on the client's own retries. Pass a key stored with the
                operation to make retries after a restart safe; Space must then
                advertise ``idempotency-key`` (``SpaceFeatureUnsupportedError``
                otherwise, nothing sent).

        Returns:
            Contract | None: Updated contract on success, otherwise None.
        """
        return self._run(
            operations.update_contract_usage_levels(
                user_id, service_name, usage_levels_novations, idempotency_key
            )
        )

    def remove_contract(self, user_id: str) -> Result[None]:
        """Delete a user contract.

        Args:
            user_id (str): User identifier.

        Returns:
            None: No value is returned.
        """
        return self._run(operations.remove_contract(user_id))

    def get_pricings(self, user_id: str) -> Result[ContractPricings]:
        """Get the pricing version of every service of a user's contract.

        Calls ``GET /contracts/{userId}/pricings``. Use it to render the pricing
        page of a signed-in customer with the version of *their* contract; each
        service also says which version new contracts get. Never cached.

        Args:
            user_id (str): User identifier.

        Returns:
            ContractPricings: Versions per service and the current
            ``contract_version``.

        Raises:
            SpaceApiError: 404 when the contract is not in the caller's organisation.
        """
        return self._run(operations.get_contract_pricings(user_id))

    # Java compatibility aliases
    getContract = get_contract
    addContract = add_contract
    updateContractSubscription = update_contract_subscription
    updateContractSubscriptionByGroupId = update_contract_subscription_by_group_id
    updateContractUsageLevels = update_contract_usage_levels
    removeContract = remove_contract
    getPricings = get_pricings
    previewNovation = preview_novation


class ContractModule(_ContractCalls, SyncRunner):
    """Contract operations over the synchronous client."""


class AsyncContractModule(_ContractCalls, AsyncRunner):
    """Contract operations over the asynchronous client.

    Same methods and same arguments as :class:`ContractModule`; every one of
    them must be awaited.
    """
