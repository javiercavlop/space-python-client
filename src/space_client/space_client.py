from __future__ import annotations

import time
from typing import Any, Callable

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
            Joins ``/contracts`` too when a contract event listener is registered.
        """
        if self._socket_is_current():
            return
        try:
            if self._socket.connected:
                # A namespace is being added: python-socketio joins namespaces
                # when it connects, so connect again with the new set.
                self._socket.disconnect()
            arguments = self._connect_arguments()
            self._socket.connect(self._ws_url(), **arguments)
            self._socket_namespaces = arguments["namespaces"]
        except Exception:
            # The client should still be usable in HTTP-only mode.
            return

    def on(self, event: str, callback: Callable[[Any], None]) -> None:
        """Register an event listener callback.

        Registering the first contract event (``contract_created``,
        ``contract_novated``, ``contract_deleted``, ``contract_renewed``) joins
        the authenticated ``/contracts`` namespace, reconnecting the socket if
        it was open. The affected user's cache entries are invalidated before
        a contract callback runs.

        Args:
            event (str): Event name (see :meth:`BaseSpaceClient.on`).
            callback (Callable[[Any], None]): Function invoked with event payload.

        Returns:
            None: Listener registration updates internal callback registry.
        """
        super().on(event, callback)
        if event.lower() in self._CONTRACT_EVENTS and self._socket_namespaces:
            self.connect()

    def disconnect(self) -> None:
        """Close active WebSocket connection.

        Returns:
            None: No value is returned.
        """
        try:
            if self._socket.connected:
                self._socket.disconnect()
            self._socket_namespaces = []
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

    def has_capability(self, capability: str) -> bool:
        """Whether the connected Space advertises a capability token.

        Uses the ``X-Space-Capabilities`` of a response seen less than
        ``CAPABILITIES_TTL_SECONDS`` ago; when there is none, reads them from
        ``GET /healthcheck`` (unauthenticated). False when they cannot be read.

        Args:
            capability (str): Token such as ``idempotency-key`` or
                ``atomic-usage-limits``.

        Returns:
            bool: True when Space advertises it.
        """
        if self._cached_capabilities() is None:
            self._probe_capabilities()
        return self._capability_in_cache(capability)

    def _probe_capabilities(self) -> None:
        """Learn the capabilities from the healthcheck; failures leave them unknown."""
        try:
            self._send("GET", "/healthcheck")
        except SpaceConnectionError:
            return

    def _sleep(self, seconds: float) -> None:
        """Wait between retries (replaced in tests)."""
        time.sleep(seconds)

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
            response = self._http_client.request(
                method,
                url,
                headers=self._headers(headers),
                json=json,
            )
        except Exception as error:
            raise SpaceConnectionError(f"Could not reach Space at {url}: {error}") from error
        self._observe_capabilities(response)
        return response

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
    hasCapability = has_capability
    isConnectedToSpace = is_connected_to_space
