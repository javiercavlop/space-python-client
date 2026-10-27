from __future__ import annotations

from typing import Any

from .. import _operations as operations
from .._result import Result
from ..types.models import Contract, ContractPricings, ContractToCreate, Subscription
from ._runner import AsyncRunner, SyncRunner


class _ContractCalls:
    """The contract calls, written once.

    Each method says what to do and hands it to ``self._run``, which is the only
    part that differs between the synchronous and asynchronous clients. On
    :class:`ContractModule` these methods return their value; on
    :class:`AsyncContractModule` they return an awaitable of that value.
    """

    _run: Any

    def get_contract(self, user_id: str) -> Result[Contract | None]:
        """Get a contract by user identifier.

        Args:
            user_id (str): User identifier.

        Returns:
            Contract | None: Parsed contract on success, otherwise None.
        """
        return self._run(operations.get_contract(user_id))

    def add_contract(self, contract_to_create: ContractToCreate) -> Result[Contract | None]:
        """Create a contract in Space.

        Args:
            contract_to_create (ContractToCreate): Contract creation payload.

        Returns:
            Contract | None: Created contract on success, otherwise None.
        """
        return self._run(operations.add_contract(contract_to_create))

    def update_contract_subscription(
        self, user_id: str, new_subscription: Subscription
    ) -> Result[Contract | None]:
        """Update subscription for a single user.

        Args:
            user_id (str): User identifier.
            new_subscription (Subscription): Subscription update payload.

        Returns:
            Contract | None: Updated contract on success, otherwise None.
        """
        return self._run(operations.update_contract_subscription(user_id, new_subscription))

    def update_contract_subscription_by_group_id(
        self,
        group_id: str,
        new_subscription: Subscription,
    ) -> Result[list[Contract] | None]:
        """Update subscription for every contract in a group.

        Args:
            group_id (str): Group identifier.
            new_subscription (Subscription): Subscription update payload.

        Returns:
            list[Contract] | None: Updated contracts list on success, otherwise None.
        """
        return self._run(
            operations.update_contract_subscription_by_group_id(group_id, new_subscription)
        )

    def update_contract_usage_levels(
        self,
        user_id: str,
        service_name: str,
        usage_levels_novations: dict[str, int | float],
    ) -> Result[Contract | None]:
        """Update usage levels for one user service.

        Args:
            user_id (str): User identifier.
            service_name (str): Service name used as payload root key.
            usage_levels_novations (dict[str, int | float]): Feature usage deltas by key.

        Returns:
            Contract | None: Updated contract on success, otherwise None.
        """
        return self._run(
            operations.update_contract_usage_levels(user_id, service_name, usage_levels_novations)
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


class ContractModule(_ContractCalls, SyncRunner):
    """Contract operations over the synchronous client."""


class AsyncContractModule(_ContractCalls, AsyncRunner):
    """Contract operations over the asynchronous client.

    Same methods and same arguments as :class:`ContractModule`; every one of
    them must be awaited.
    """
