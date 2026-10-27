from __future__ import annotations

from typing import Any

from .. import _operations as operations
from .._result import Result
from ..types.models import NewContractsPricing
from ._runner import AsyncRunner, SyncRunner


class _ServiceCalls:
    """The service calls, written once (see :mod:`space_client.modules._runner`)."""

    _run: Any

    def get_new_contracts_pricing(self, service_name: str) -> Result[NewContractsPricing]:
        """Get the pricing version Space assigns to NEW contracts of a service.

        Calls ``GET /services/{serviceName}/pricings/new-contracts``. Use it to
        render the pricing page for signed-out visitors.

        Args:
            service_name (str): Service name.

        Returns:
            NewContractsPricing: The version, the path of its YAML and how it
            was chosen (``sphere-policy``, ``default`` or ``latest-active``).
        """
        return self._run(operations.get_new_contracts_pricing(service_name))

    # Java compatibility aliases
    getNewContractsPricing = get_new_contracts_pricing


class ServiceModule(_ServiceCalls, SyncRunner):
    """Service reads over the synchronous client."""


class AsyncServiceModule(_ServiceCalls, AsyncRunner):
    """Service reads over the asynchronous client; every method must be awaited."""
