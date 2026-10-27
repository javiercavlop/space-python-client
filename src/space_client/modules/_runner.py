"""Running an :class:`~space_client._operations.Operation`, either way.

This is the seam between the two clients. A module's methods say *what* to do by
building an operation; these two runners are the only code that knows whether
sending it blocks or is awaited.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from .._operations import Operation, Work
from ..errors import SpaceApiError

if TYPE_CHECKING:
    from .cache_module import CacheModule

T = TypeVar("T")


class ModuleBase:
    """State and cache handling shared by every module, sync or async."""

    def __init__(self, space_client: Any) -> None:
        """Create an operations module.

        Args:
            space_client: Parent client used for transport and cache.

        Returns:
            None: Constructor only stores references.
        """
        self._space_client = space_client

    @property
    def _cache(self) -> "CacheModule":
        return self._space_client.cache

    def _capabilities(self) -> "frozenset[str] | None":
        return self._space_client._cached_capabilities()

    def _cached(self, operation: Operation[T]) -> T | None:
        """What the cache can answer on its own, if anything.

        Kept out of both runners because deciding whether to look is the same
        decision in both, and getting it wrong in one of them would be a
        difference nobody would see until a stale entitlement was served.
        """
        if operation.cache_hit is None or not self._cache.is_enabled():
            return None
        return operation.cache_hit(self._cache)

    def _refused(self, operation: Operation[Any], error: SpaceApiError) -> None:
        """Give the operation a chance to say what a refusal means, then re-raise."""
        if operation.on_refusal is not None:
            operation.on_refusal(self._cache, error)


class SyncRunner(ModuleBase):
    def _run(self, work: Work[T]) -> T:
        if isinstance(work, Operation):
            return self._run_one(work)
        # A flow: send each operation, hand its result back, until it returns.
        try:
            operation = next(work)
            while True:
                operation = work.send(self._run_one(operation))
        except StopIteration as done:
            return done.value

    def _run_one(self, operation: Operation[T]) -> T:
        cached = self._cached(operation)
        if cached is not None:
            return cached

        client = self._space_client
        try:
            if operation.raw:
                answer = client._request_raw(
                    operation.method,
                    operation.path,
                    json=operation.json,
                    headers=operation.headers,
                    accept=operation.accept,
                )
            elif operation.no_content:
                answer = client._request_no_content(
                    operation.method, operation.path, json=operation.json, headers=operation.headers
                )
            else:
                answer = client._request_json(
                    operation.method, operation.path, json=operation.json, headers=operation.headers
                )
        except SpaceApiError as error:
            self._refused(operation, error)
            raise
        return operation.parse(self._cache, answer)


class AsyncRunner(ModuleBase):
    async def _run(self, work: Work[T]) -> T:
        if isinstance(work, Operation):
            return await self._run_one(work)
        try:
            operation = next(work)
            while True:
                operation = work.send(await self._run_one(operation))
        except StopIteration as done:
            return done.value

    async def _run_one(self, operation: Operation[T]) -> T:
        cached = self._cached(operation)
        if cached is not None:
            return cached

        client = self._space_client
        try:
            if operation.raw:
                answer = await client._request_raw(
                    operation.method,
                    operation.path,
                    json=operation.json,
                    headers=operation.headers,
                    accept=operation.accept,
                )
            elif operation.no_content:
                answer = await client._request_no_content(
                    operation.method, operation.path, json=operation.json, headers=operation.headers
                )
            else:
                answer = await client._request_json(
                    operation.method, operation.path, json=operation.json, headers=operation.headers
                )
        except SpaceApiError as error:
            self._refused(operation, error)
            raise
        return operation.parse(self._cache, answer)
