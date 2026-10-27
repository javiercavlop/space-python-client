"""Everything a Space client does that has nothing to do with waiting.

Validating options, building URLs, remembering event callbacks, deciding whether
a response was a refusal, decoding a body. None of it blocks, so none of it needs
to exist twice.

:class:`SpaceClient` and :class:`AsyncSpaceClient` add the part that does: how a
request is sent, and how the socket is opened.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .errors import SpaceApiError
from .types.space_connection_options import SpaceConnectionOptions
from .types.space_event import SpaceEvent


def _read_body(response) -> Any:
    """Whatever Space sent with a refusal, JSON if it parses and text if not.

    Never raises: this runs while an error is being built, and failing here
    would replace a useful message with a confusing one.
    """
    try:
        return response.json()
    except Exception:
        try:
            return response.text
        except Exception:
            return None


def _now() -> float:
    return time.monotonic()


PRICINGS_NAMESPACE = "/pricings"
CONTRACTS_NAMESPACE = "/contracts"


class BaseSpaceClient:
    CAPABILITIES_TTL_SECONDS = 60
    """How long the ``X-Space-Capabilities`` of a response are trusted."""

    _CONTRACT_EVENTS = frozenset(
        {
            SpaceEvent.CONTRACT_CREATED.value,
            SpaceEvent.CONTRACT_NOVATED.value,
            SpaceEvent.CONTRACT_DELETED.value,
            SpaceEvent.CONTRACT_RENEWED.value,
        }
    )
    _VALID_EVENTS = {
        SpaceEvent.SYNCHRONIZED.value,
        SpaceEvent.PRICING_CREATED.value,
        SpaceEvent.PRICING_ARCHIVED.value,
        SpaceEvent.PRICING_ACTIVED.value,
        SpaceEvent.SERVICE_DISABLED.value,
        SpaceEvent.ERROR.value,
        *_CONTRACT_EVENTS,
    }

    def __init__(self, options: SpaceConnectionOptions) -> None:
        """Set up the parts of a client that do not depend on the transport.

        Args:
            options (SpaceConnectionOptions): Connection settings including URL, API key,
                timeout, and optional cache configuration.

        Returns:
            None: This constructor initializes client state.

        Raises:
            ValueError: If URL or API key are missing.
        """
        if not options.url:
            raise ValueError("URL is required")
        if not options.api_key:
            raise ValueError("API key is required")

        self._base_url = options.url.rstrip("/")
        self._http_url = f"{self._base_url}/api/v1"
        self._api_key = options.api_key
        self._timeout = options.timeout
        self._callbacks: dict[str, Callable[[Any], None]] = {}
        # The capabilities last advertised by Space, and when (see S0).
        self._capabilities: tuple[frozenset[str], float] | None = None
        self._socket: Any = None
        self.cache: Any = None
        # The namespaces the socket was last connected with: adding one to a
        # python-socketio connection means connecting again.
        self._socket_namespaces: list[str] = []

    # -- events ------------------------------------------------------------

    def _configure_socket_handlers(self) -> None:
        @self._socket.on("connect", namespace="/pricings")
        def _on_connect() -> None:
            callback = self._callbacks.get(SpaceEvent.SYNCHRONIZED.value)
            if callback is not None:
                callback(None)

        @self._socket.on("message", namespace="/pricings")
        def _on_message(data: Any) -> None:
            if not isinstance(data, dict):
                return
            code = str(data.get("code", "")).lower()
            details = data.get("details")
            if code in self._CONTRACT_EVENTS:
                # Contract events come only from the authenticated namespace.
                return
            callback = self._callbacks.get(code)
            if callback is not None:
                callback(details)

        @self._socket.on("connect_error", namespace="/pricings")
        def _on_connect_error(error: Any) -> None:
            callback = self._callbacks.get(SpaceEvent.ERROR.value)
            if callback is not None:
                callback(error)

        @self._socket.on("message", namespace=CONTRACTS_NAMESPACE)
        def _on_contract_message(data: Any) -> None:
            if not isinstance(data, dict):
                return
            code = str(data.get("code", "")).lower()
            details = data.get("details")
            # Whatever we cached for that user is stale now; drop it before
            # the callback runs, so a callback that re-reads gets fresh data.
            user_id = details.get("userId") if isinstance(details, dict) else None
            if user_id and self.cache is not None:
                self.cache.invalidate_user(str(user_id))
            if code not in self._CONTRACT_EVENTS:
                return
            callback = self._callbacks.get(code)
            if callback is not None:
                callback(details)

        @self._socket.on("connect_error", namespace=CONTRACTS_NAMESPACE)
        def _on_contract_connect_error(error: Any) -> None:
            callback = self._callbacks.get(SpaceEvent.ERROR.value)
            if callback is not None:
                callback(error)

    def _has_contract_listeners(self) -> bool:
        return any(event in self._callbacks for event in self._CONTRACT_EVENTS)

    def _wanted_namespaces(self) -> list[str]:
        if self._has_contract_listeners():
            return [PRICINGS_NAMESPACE, CONTRACTS_NAMESPACE]
        return [PRICINGS_NAMESPACE]

    def _socket_is_current(self) -> bool:
        return bool(self._socket.connected) and set(self._wanted_namespaces()) <= set(
            self._socket_namespaces
        )

    def _connect_arguments(self) -> dict[str, Any]:
        """What ``socket.connect`` is called with.

        ``/contracts`` only answers authenticated callers: the API key goes in
        the Socket.IO ``auth`` payload and, for servers that read the
        handshake, the ``x-api-key`` header. ``/pricings`` stays anonymous
        unless contracts are wanted too, since both share one connection.
        """
        namespaces = self._wanted_namespaces()
        arguments: dict[str, Any] = {
            "namespaces": namespaces,
            "socketio_path": "events",
            "wait": False,
        }
        if CONTRACTS_NAMESPACE in namespaces:
            arguments["auth"] = {"apiKey": self._api_key}
            arguments["headers"] = {"x-api-key": self._api_key}
        return arguments

    def on(self, event: str, callback: Callable[[Any], None]) -> None:
        """Register an event listener callback.

        Args:
            event (str): Event name. Supported values include synchronized,
                pricing_created, pricing_archived, pricing_actived,
                service_disabled, contract_created, contract_novated,
                contract_deleted, contract_renewed, and error. Contract events
                are received on the authenticated ``/contracts`` namespace, only
                for the API key's organisation.
            callback (Callable[[Any], None]): Function invoked with event payload.

        Returns:
            None: Listener registration updates internal callback registry.
        """
        event_lower = event.lower()
        if event_lower in self._VALID_EVENTS:
            self._callbacks[event_lower] = callback

    def remove_listener(self, event: str) -> None:
        """Remove the callback associated with an event.

        Args:
            event (str): Event name to deregister.

        Returns:
            None: No value is returned.
        """
        event_lower = event.lower()
        if event_lower in self._VALID_EVENTS:
            self._callbacks.pop(event_lower, None)

    def remove_all_listeners(self) -> None:
        """Remove all registered callbacks.

        Returns:
            None: No value is returned.
        """
        self._callbacks.clear()

    # -- capabilities (S0) ----------------------------------------------------

    def _observe_capabilities(self, response: Any) -> None:
        """Remember what a response says this Space implements.

        A successful answer without ``X-Space-Capabilities`` comes from a Space
        that predates the header, i.e. one with none of those features. A
        refusal without it is ignored: it may come from a proxy.
        """
        headers = getattr(response, "headers", None)
        raw = headers.get("x-space-capabilities") if headers is not None else None
        status = getattr(response, "status_code", 0)
        if raw is None and not 200 <= status < 400:
            return
        tokens = frozenset(
            token.strip().lower() for token in str(raw or "").split(",") if token.strip()
        )
        self._capabilities = (tokens, _now())

    def _cached_capabilities(self) -> frozenset[str] | None:
        """The capabilities seen less than ``CAPABILITIES_TTL_SECONDS`` ago, or None."""
        known = self._capabilities
        if known is None or _now() - known[1] >= self.CAPABILITIES_TTL_SECONDS:
            return None
        return known[0]

    def clear_capabilities(self) -> None:
        """Forget the cached capabilities, e.g. right after upgrading Space.

        Returns:
            None: The next guarded call asks Space again.
        """
        self._capabilities = None

    # -- responses ---------------------------------------------------------

    def _ws_url(self) -> str:
        return self._base_url.replace("http://", "ws://").replace("https://", "wss://")

    def _refuse(
        self, response, method: str, path: str, accept: frozenset[int] = frozenset()
    ) -> None:
        """Raise if Space refused, so a refusal is never mistaken for an answer.

        ``accept`` names statuses outside 2xx that are answers for this call,
        such as ``304 Not Modified`` for a conditional read.
        """
        if response.status_code in accept:
            return
        if response.status_code < 200 or response.status_code >= 300:
            raise SpaceApiError(response.status_code, method, path, _read_body(response))

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"x-api-key": self._api_key}
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _decode(response) -> dict[str, Any] | list[Any] | None:
        """The decoded body, or None when Space answered with something else.

        ``None`` means one thing - a successful response whose body is not JSON -
        instead of standing in for a refusal, an outage and an empty response at
        once.
        """
        try:
            return response.json()
        except Exception:
            return None

    # -- accessors ---------------------------------------------------------

    def get_http_url(self) -> str:
        """Get internal API URL.

        Returns:
            str: Base HTTP URL including `/api/v1` suffix.
        """
        return self._http_url

    def get_api_key(self) -> str:
        """Get configured API key.

        Returns:
            str: API key currently used by the client.
        """
        return self._api_key

    def get_timeout(self) -> int:
        """Get configured timeout.

        Returns:
            int: HTTP timeout in milliseconds.
        """
        return self._timeout

    # Java compatibility aliases
    removeListener = remove_listener
    removeAllListeners = remove_all_listeners
    getHttpUrl = get_http_url
    getApiKey = get_api_key
    getTimeout = get_timeout
    clearCapabilities = clear_capabilities
