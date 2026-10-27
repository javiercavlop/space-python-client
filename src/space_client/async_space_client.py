from __future__ import annotations

from typing import Any

import httpx
import socketio

from . import _operations as operations
from ._base_client import BaseSpaceClient
from .errors import SpaceConnectionError
from .modules.cache_module import CacheModule
from .modules.contract_module import AsyncContractModule
from .modules.feature_module import AsyncFeatureModule
from .modules.pricing_module import AsyncPricingModule
from .modules.service_module import AsyncServiceModule
from .types.space_connection_options import SpaceConnectionOptions


class AsyncSpaceClient(BaseSpaceClient):
    """The same client, for code that runs in an event loop.

    Entitlement checks sit on the request path: a service asks Space "may this
    user do this?" before serving a request. Done synchronously inside an event
    loop, that call blocks every other request the process is serving for as long
    as it takes - including the full timeout, when Space is slow or unreachable.

    Same modules, same method names, same arguments and same exceptions as
    :class:`~space_client.space_client.SpaceClient`; the calls are awaited. The
    logic behind them is shared rather than copied, so the two cannot drift.

    Unlike the synchronous client, this one does not open its WebSocket in the
    constructor, because a constructor cannot await. Call :meth:`connect`, or use
    the client as an async context manager, if you want pricing events; HTTP
    calls work either way.

        async with AsyncSpaceClient(options) as space:
            result = await space.features.evaluate(user_id, "petclinic-pets")
    """

    def __init__(self, options: SpaceConnectionOptions) -> None:
        """Create a new asynchronous Space client.

        Args:
            options (SpaceConnectionOptions): Connection settings including URL, API key,
                timeout, and optional cache configuration.

        Returns:
            None: This constructor initializes client state and modules. It does
            not open the WebSocket connection; see :meth:`connect`.

        Raises:
            ValueError: If URL or API key are missing.
        """
        super().__init__(options)

        self._http_client = httpx.AsyncClient(timeout=self._timeout / 1000)

        self.cache = CacheModule()
        self.contracts = AsyncContractModule(self)
        self.features = AsyncFeatureModule(self)
        self.pricings = AsyncPricingModule(self)
        self.services = AsyncServiceModule(self)

        if options.cache is not None and options.cache.enabled:
            try:
                self.cache.initialize(options.cache)
            except Exception:
                pass

        self._socket = socketio.AsyncClient(
            reconnection=True, logger=False, engineio_logger=False
        )
        self._configure_socket_handlers()

    async def is_connected_to_space(self) -> bool:
        """Check Space health endpoint availability.

        Returns:
            bool: True when `/healthcheck` responds successfully with a `message` field,
            otherwise False.
        """
        operation = operations.healthcheck()
        payload = await self._request_json(operation.method, operation.path)
        return operation.parse(self.cache, payload)

    async def connect(self) -> None:
        """Open the WebSocket connection to Space pricing events.

        Returns:
            None: Failures are ignored, leaving the client usable over HTTP.
            Joins ``/contracts`` too when a contract event listener is
            registered; ``on`` cannot await, so register contract listeners
            before connecting, or call ``connect()`` again afterwards.
        """
        if self._socket_is_current():
            return
        try:
            if self._socket.connected:
                # A namespace is being added: python-socketio joins namespaces
                # when it connects, so connect again with the new set.
                await self._socket.disconnect()
            arguments = self._connect_arguments()
            await self._socket.connect(self._ws_url(), **arguments)
            self._socket_namespaces = arguments["namespaces"]
        except Exception:
            # The client should still be usable in HTTP-only mode.
            return

    async def disconnect(self) -> None:
        """Close active WebSocket connection.

        Returns:
            None: No value is returned.
        """
        try:
            if self._socket.connected:
                await self._socket.disconnect()
            self._socket_namespaces = []
        except Exception:
            return

    async def close(self) -> None:
        """Release all client resources.

        Returns:
            None: No value is returned.
        """
        await self.disconnect()
        self.cache.close()
        await self._http_client.aclose()

    async def __aenter__(self) -> "AsyncSpaceClient":
        await self.connect()
        return self

    async def __aexit__(self, *_exc_info: Any) -> None:
        await self.close()

    async def _send(
        self,
        method: str,
        path: str,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ):
        """Make the request, or say why it could not be made.

        A transport failure is not an answer, so it is raised rather than folded
        into one: a caller that cannot reach Space has learnt something different
        from a caller whose request was refused.
        """
        url = f"{self._http_url}{path}"
        try:
            response = await self._http_client.request(
                method,
                url,
                headers=self._headers(headers),
                json=json,
            )
        except Exception as error:
            raise SpaceConnectionError(f"Could not reach Space at {url}: {error}") from error
        self._observe_capabilities(response)
        return response

    async def _request_json(
        self,
        method: str,
        path: str,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | list[Any] | None:
        """The decoded body, or an exception saying what Space objected to."""
        response = await self._send(method, path, json=json, headers=headers)
        self._refuse(response, method, path)
        return self._decode(response)

    async def _request_no_content(
        self,
        method: str,
        path: str,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> bool:
        """Whether Space accepted a request that returns nothing."""
        response = await self._send(method, path, json=json, headers=headers)
        self._refuse(response, method, path)
        return True

    async def _request_raw(
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
        response = await self._send(method, path, json=json, headers=headers)
        self._refuse(response, method, path, accept)
        return response

    # Java compatibility aliases
    isConnectedToSpace = is_connected_to_space
