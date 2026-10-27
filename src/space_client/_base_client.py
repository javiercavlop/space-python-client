"""Everything a Space client does that has nothing to do with waiting.

Validating options, building URLs, remembering event callbacks, deciding whether
a response was a refusal, decoding a body. None of it blocks, so none of it needs
to exist twice.

:class:`SpaceClient` and :class:`AsyncSpaceClient` add the part that does: how a
request is sent, and how the socket is opened.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Literal

from .errors import SpaceApiError
from .event_id_window import EventIdWindow
from .types.event_details import EventDeduplicationOptions, EventDetails, ResyncInfo
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


def _event_id_window(option: EventDeduplicationOptions | bool | None) -> EventIdWindow | None:
    if option is None or option is True:
        return EventIdWindow()
    if option is False:
        return None
    if not option.enabled:
        return None
    return EventIdWindow(max_entries=option.max_entries, window_seconds=option.window_seconds)


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
        SpaceEvent.RESYNC.value,
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
        self._organization_id = getattr(options, "organization_id", None)
        self._timeout = options.timeout
        self._callbacks: dict[str, Callable[[Any], None]] = {}
        # The capabilities last advertised by Space, and when (see S0).
        self._capabilities: tuple[frozenset[str], float] | None = None
        self._socket: Any = None
        self.cache: Any = None
        # The namespaces the socket was last connected with: adding one to a
        # python-socketio connection means connecting again.
        self._socket_namespaces: list[str] = []
        # The eventIds seen recently, shared by both namespaces (None: off).
        self._event_ids = _event_id_window(getattr(options, "event_deduplication", True))
        # Whether each namespace connected before, to tell a reconnection.
        self._connected_before = {"pricings": False, "contracts": False}

    # -- events ------------------------------------------------------------

    def _configure_socket_handlers(self) -> None:
        @self._socket.on("connect", namespace="/pricings")
        def _on_connect() -> None:
            callback = self._callbacks.get(SpaceEvent.SYNCHRONIZED.value)
            if callback is not None:
                callback(None)
            self._emit_resync("pricings")

        @self._socket.on("message", namespace="/pricings")
        def _on_message(data: Any) -> None:
            if not isinstance(data, dict):
                return
            code = str(data.get("code", "")).lower()
            if code in self._CONTRACT_EVENTS:
                # Contract events come only from the authenticated namespace.
                return
            accepted, details = self._accept_event(data)
            if not accepted:
                return
            callback = self._callbacks.get(code)
            if callback is not None:
                callback(details)

        @self._socket.on("connect_error", namespace="/pricings")
        def _on_connect_error(error: Any) -> None:
            callback = self._callbacks.get(SpaceEvent.ERROR.value)
            if callback is not None:
                callback(error)

        @self._socket.on("connect", namespace=CONTRACTS_NAMESPACE)
        def _on_contract_connect() -> None:
            if self._connected_before["contracts"] and self.cache is not None:
                # Contract events sent while disconnected are lost, so what
                # the cache holds for any user may be stale.
                self.cache.invalidate_all_users()
            self._emit_resync("contracts")

        @self._socket.on("message", namespace=CONTRACTS_NAMESPACE)
        def _on_contract_message(data: Any) -> None:
            if not isinstance(data, dict):
                return
            code = str(data.get("code", "")).lower()
            accepted, details = self._accept_event(data)
            if not accepted:
                return
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

    def _accept_event(self, data: dict[str, Any]) -> tuple[bool, Any]:
        """Whether to deliver an event, and the details to deliver.

        A repeat of an ``eventId`` seen in the deduplication window is
        dropped; an event without ``eventId`` (older Space) always passes.
        Dict details become :class:`EventDetails`, which carry ``event_id`` and
        ``occurred_at``.
        """
        event_id = data.get("eventId")
        if not isinstance(event_id, str) or not event_id:
            event_id = None
        if (
            event_id is not None
            and self._event_ids is not None
            and not self._event_ids.accept(event_id)
        ):
            return False, None
        details = data.get("details")
        if isinstance(details, dict):
            occurred_at = data.get("occurredAt")
            details = EventDetails(
                details,
                code=str(data.get("code", "")),
                event_id=event_id,
                raw_occurred_at=occurred_at if isinstance(occurred_at, str) else None,
            )
        return True, details

    def _emit_resync(self, namespace: Literal["pricings", "contracts"]) -> None:
        info = ResyncInfo(namespace=namespace, reconnect=self._connected_before[namespace])
        self._connected_before[namespace] = True
        callback = self._callbacks.get(SpaceEvent.RESYNC.value)
        if callback is not None:
            callback(info)

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
        the Socket.IO ``auth`` payload (with ``organizationId`` when one is
        configured, as member user keys need) and, for servers that read the
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
            auth = {"apiKey": self._api_key}
            if self._organization_id:
                # Member user keys act in an organisation they name.
                auth["organizationId"] = self._organization_id
            arguments["auth"] = auth
            arguments["headers"] = {"x-api-key": self._api_key}
        return arguments

    def on(self, event: str, callback: Callable[[Any], None]) -> None:
        """Register an event listener callback.

        Args:
            event (str): Event name. Supported values include synchronized,
                pricing_created, pricing_archived, pricing_actived,
                service_disabled, contract_created, contract_novated,
                contract_deleted, contract_renewed, resync, and error. Contract
                events are received on the authenticated ``/contracts``
                namespace, only for the API key's organisation. ``resync``
                fires each time a namespace connects, with a
                :class:`ResyncInfo`; after a reconnection, re-read the
                contracts you hold, since Space does not replay events.
            callback (Callable[[Any], None]): Function invoked with event payload.
                Pricing and contract listeners get :class:`EventDetails` (the
                ``details`` dict plus ``event_id`` and ``occurred_at``); a
                repeated ``eventId`` is not delivered again.

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
