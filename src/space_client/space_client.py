from __future__ import annotations

from typing import Any

import httpx
import socketio

from . import _operations as operations
from ._base_client import BaseSpaceClient, _read_body  # noqa: F401  (kept importable)
from .errors import SpaceConnectionError
from .modules.cache_module import CacheModule
from .modules.contract_module import ContractModule
from .modules.feature_module import FeatureModule
from .modules.pricing_module import PricingModule
from .modules.service_module import ServiceModule
from .types.space_connection_options import SpaceConnectionOptions


class SpaceClient(BaseSpaceClient):
    def __init__(self, options: SpaceConnectionOptions) -> None:
        """Create a new Space client.

        Args:
            options (SpaceConnectionOptions): Connection settings including URL, API key,
                timeout, and optional cache configuration.

        Returns:
            None: This constructor initializes client state and modules.

        Raises:
            ValueError: If URL or API key are missing.
        """
        super().__init__(options)

        self._http_client = httpx.Client(timeout=self._timeout / 1000)

        self.cache = CacheModule()
        self.contracts = ContractModule(self)
        self.features = FeatureModule(self)
        self.pricings = PricingModule(self)
        self.services = ServiceModule(self)

        if options.cache is not None and options.cache.enabled:
            try:
                self.cache.initialize(options.cache)
            except Exception:
                pass

        self._socket = socketio.Client(reconnection=True, logger=False, engineio_logger=False)
        self._configure_socket_handlers()
        self.connect()

    def is_connected_to_space(self) -> bool:
        """Check Space health endpoint availability.

        Returns:
            bool: True when `/healthcheck` responds successfully with a `message` field,
            otherwise False.
        """
        operation = operations.healthcheck()
        payload = self._request_json(operation.method, operation.path)
        return operation.parse(self.cache, payload)

    def connect(self) -> None:
        """Open the WebSocket connection to Space pricing events.

        Returns:
            None: Connection is attempted asynchronously; failures are ignored.
        """
        if self._socket.connected:
            return
        try:
            self._socket.connect(
                self._ws_url(), namespaces=["/pricings"], socketio_path="events", wait=False
            )
        except Exception:
            # The client should still be usable in HTTP-only mode.
            return

    def disconnect(self) -> None:
        """Close active WebSocket connection.

        Returns:
            None: No value is returned.
        """
        try:
            if self._socket.connected:
                self._socket.disconnect()
        except Exception:
            return

    def close(self) -> None:
        """Release all client resources.

        Returns:
            None: No value is returned.
        """
        self.disconnect()
        self.cache.close()
        self._http_client.close()

    def __enter__(self) -> "SpaceClient":
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.close()

    def _send(
        self,
        method: str,
        path: str,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ):
        """Make the request, or say why it could not be made.

        A transport failure is not an answer, so it is raised rather than
        folded into one: a caller that cannot reach Space has learnt something
        different from a caller whose request was refused.
        """
        url = f"{self._http_url}{path}"
        try:
            return self._http_client.request(
                method,
                url,
                headers=self._headers(headers),
                json=json,
            )
        except Exception as error:
            raise SpaceConnectionError(f"Could not reach Space at {url}: {error}") from error

    def _request_json(
        self,
        method: str,
        path: str,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | list[Any] | None:
        """The decoded body, or an exception saying what Space objected to."""
        response = self._send(method, path, json=json, headers=headers)
        self._refuse(response, method, path)
        return self._decode(response)

    def _request_no_content(
        self,
        method: str,
        path: str,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> bool:
        """Whether Space accepted a request that returns nothing.

        Still a boolean, because for these calls "did it work" is the whole
        question - but a refusal raises, so ``False`` is not the answer to both
        "it declined" and "it never arrived".
        """
        response = self._send(method, path, json=json, headers=headers)
        self._refuse(response, method, path)
        return True

    def _request_raw(
        self,
        method: str,
        path: str,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        accept: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        """The response itself, for calls whose answer is more than a JSON body.

        A YAML file, an ``ETag`` or a ``304`` would all be lost by decoding.
        Refusals still raise; ``accept`` lists the non-2xx statuses that are
        answers for this call.
        """
        response = self._send(method, path, json=json, headers=headers)
        self._refuse(response, method, path, accept)
        return response

    # Java compatibility aliases
    isConnectedToSpace = is_connected_to_space
