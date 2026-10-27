from __future__ import annotations

from typing import Any

from .. import _operations as operations
from .._result import Result
from ..types.models import PricingYaml
from ._runner import AsyncRunner, SyncRunner


class _PricingCalls:
    """The pricing calls, written once (see :mod:`space_client.modules._runner`)."""

    _run: Any

    def get_yaml(
        self, service_name: str, pricing_version: str, etag: str | None = None
    ) -> Result[PricingYaml]:
        """Get the raw Pricing2Yaml file of a pricing version, byte for byte.

        Calls ``GET /services/{serviceName}/pricings/{pricingVersion}?format=yaml``.
        Pass the ``etag`` of a copy you already hold to make a conditional
        request: if the file has not changed Space answers 304 and the result
        has ``not_modified=True`` and ``yaml=None``.

        Args:
            service_name (str): Service name.
            pricing_version (str): Pricing version, e.g. ``"2024-06-08"``.
            etag (str | None): ETag of a cached copy, quoted or not.

        Returns:
            PricingYaml: The file, its ETag and its syntax version.

        Raises:
            PricingYamlUnavailableError: Space cannot provide the original file.
            SpaceApiError: Any other refusal (e.g. 404 unknown version).
        """
        return self._run(operations.get_pricing_yaml(service_name, pricing_version, etag))

    # Java compatibility aliases
    getYaml = get_yaml


class PricingModule(_PricingCalls, SyncRunner):
    """Pricing reads over the synchronous client."""


class AsyncPricingModule(_PricingCalls, AsyncRunner):
    """Pricing reads over the asynchronous client; every method must be awaited."""
