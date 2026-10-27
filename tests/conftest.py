from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from space_client import AsyncSpaceClient, SpaceClient
from space_client.types import SpaceConnectionOptions

SPACE_URL = "http://space.test"
API_URL = f"{SPACE_URL}/api/v1"


class FakeSocketClient:
    def __init__(self, *args, **kwargs):
        self.connected = False
        self._handlers: dict[tuple[str, str | None], callable] = {}
        self.connect_calls: list[dict] = []
        self.disconnect_calls = 0

    def on(self, event: str, namespace: str | None = None):
        def decorator(func):
            self._handlers[(event, namespace)] = func
            return func

        return decorator

    def connect(self, *args, **kwargs):
        self.connect_calls.append(kwargs)
        self.connected = True

    def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False

    def trigger(self, event: str, data=None, namespace: str | None = "/pricings"):
        handler = self._handlers.get((event, namespace))
        if handler is None:
            return
        if data is None:
            handler()
        else:
            handler(data)


class FakeAsyncSocketClient(FakeSocketClient):
    """The same stub, with the two methods the async socket awaits."""

    async def connect(self, *args, **kwargs):  # type: ignore[override]
        self.connect_calls.append(kwargs)
        self.connected = True

    async def disconnect(self):  # type: ignore[override]
        self.disconnect_calls += 1
        self.connected = False

    async def trigger_async(self, event: str, data=None, namespace: str | None = "/pricings"):
        handler = self._handlers[(event, namespace)]
        result = handler() if data is None else handler(data)
        if inspect.isawaitable(result):
            await result


@pytest.fixture(autouse=True)
def patch_socketio_client(monkeypatch):
    from space_client import async_space_client as async_client_module
    from space_client import space_client as client_module

    monkeypatch.setattr(client_module.socketio, "Client", FakeSocketClient)
    monkeypatch.setattr(async_client_module.socketio, "AsyncClient", FakeAsyncSocketClient)


# -- A mocked Space over real HTTP -------------------------------------------
#
# The calls added for the subscription-change API depend on more than a JSON
# body - status codes like 304 and 412, headers like ``ETag``, a YAML body - so
# they are tested against httpx's own transport mocked by ``respx`` rather than
# a hand-written ``request`` stub. No live Space is involved.
#
# ``flavour`` runs a test twice, once over ``SpaceClient`` and once over
# ``AsyncSpaceClient``, and the ``call`` handed out with the client settles a
# result either way: the two clients must behave the same, so the tests say so
# once.


@pytest.fixture(params=["sync", "async"])
def flavour(request) -> str:
    return request.param


@pytest.fixture
def space(flavour: str):
    """``(client, call)`` for the requested flavour."""
    loop = asyncio.new_event_loop() if flavour == "async" else None
    options = SpaceConnectionOptions(url=SPACE_URL, api_key="org-key")
    client: Any = AsyncSpaceClient(options) if flavour == "async" else SpaceClient(options)

    def call(result: Any) -> Any:
        if inspect.isawaitable(result):
            assert loop is not None, "a sync call must not return an awaitable"
            return loop.run_until_complete(result)
        assert loop is None, "an async call must return an awaitable"
        return result

    yield client, call

    call(client.close())
    if loop is not None:
        loop.close()
