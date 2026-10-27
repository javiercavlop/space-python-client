"""Everything a Space client does that has nothing to do with waiting.

Validating options, building URLs, remembering event callbacks, deciding whether
a response was a refusal, decoding a body. None of it blocks, so none of it needs
to exist twice.

:class:`SpaceClient` and :class:`AsyncSpaceClient` add the part that does: how a
request is sent, and how the socket is opened.
"""

from __future__ import annotations

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


class BaseSpaceClient:
    _VALID_EVENTS = {
        SpaceEvent.SYNCHRONIZED.value,
        SpaceEvent.PRICING_CREATED.value,
        SpaceEvent.PRICING_ARCHIVED.value,
        SpaceEvent.PRICING_ACTIVED.value,
        SpaceEvent.SERVICE_DISABLED.value,
        SpaceEvent.ERROR.value,
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
            callback = self._callbacks.get(code)
            if callback is not None:
                callback(details)

        @self._socket.on("connect_error", namespace="/pricings")
        def _on_connect_error(error: Any) -> None:
            callback = self._callbacks.get(SpaceEvent.ERROR.value)
            if callback is not None:
                callback(error)

    def on(self, event: str, callback: Callable[[Any], None]) -> None:
        """Register an event listener callback.

        Args:
            event (str): Event name. Supported values include synchronized,
                pricing_created, pricing_archived, pricing_actived,
                service_disabled, and error.
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
