"""Running an :class:`~space_client._operations.Operation`, either way.

This is the seam between the two clients. A module's methods say *what* to do by
building an operation; these two runners are the only code that knows whether
sending it blocks or is awaited.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from .._operations import IDEMPOTENCY_KEY, Operation, Work
from ..errors import (
    IdempotencyConflictError,
    IdempotencyError,
    IdempotencyInProgressError,
    IdempotencyKeyInvalidError,
    SpaceApiError,
    SpaceConnectionError,
    SpaceFeatureUnsupportedError,
    parse_retry_after,
)

if TYPE_CHECKING:
    from .cache_module import CacheModule

T = TypeVar("T")

_MAX_KEY_LENGTH = 255

_IDEMPOTENCY_ERRORS: dict[tuple[int, str], type[IdempotencyError]] = {
    (409, IdempotencyConflictError.code): IdempotencyConflictError,
    (409, IdempotencyInProgressError.code): IdempotencyInProgressError,
    (400, IdempotencyKeyInvalidError.code): IdempotencyKeyInvalidError,
}


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

    def _check_key(self, operation: Operation[Any]) -> None:
        key = operation.idempotency_key
        if operation.explicit_key and (
            not isinstance(key, str) or not 1 <= len(key) <= _MAX_KEY_LENGTH
        ):
            raise ValueError(f"idempotency_key must be a string of 1 to {_MAX_KEY_LENGTH} characters")

    def _preconditions(self, operation: Operation[Any]) -> list[tuple[str, str]]:
        """Capabilities to confirm before sending, with what each one is for."""
        needed = list(operation.requires)
        if operation.explicit_key:
            needed.append((IDEMPOTENCY_KEY, "Idempotency-Key"))
        return needed

    @staticmethod
    def _unsupported(capability: str, what: str) -> SpaceFeatureUnsupportedError:
        return SpaceFeatureUnsupportedError(
            capability, f"The connected Space does not support {what}: nothing was written"
        )

    @staticmethod
    def _headers_of(operation: Operation[Any]) -> dict[str, str] | None:
        if operation.idempotency_key is None:
            return operation.headers
        return {**(operation.headers or {}), "Idempotency-Key": operation.idempotency_key}

    @staticmethod
    def _idempotency_failure(operation: Operation[Any], error: Exception) -> IdempotencyError | None:
        """Space's refusal of the key, as its typed error."""
        if operation.idempotency_key is None or not isinstance(error, SpaceApiError):
            return None
        code = error.payload.get("code") if isinstance(error.payload, dict) else None
        kind = _IDEMPOTENCY_ERRORS.get((error.status_code, str(code)))
        return kind.from_error(error, operation.idempotency_key) if kind else None

    def _next_delay(
        self,
        operation: Operation[Any],
        error: Exception,
        failure: IdempotencyError | None,
        attempt: int,
    ) -> float | None:
        """Seconds to wait before retrying, or None when this failure is final.

        Only keyed writes are retried, and only when the attempt may not have
        been applied (no answer, ``5xx``) or another one with the key is still
        running (``409 IDEMPOTENCY_IN_PROGRESS``). Whether Space supports the
        key is checked by the caller, which may have to ask.
        """
        if operation.idempotency_key is None:
            return None
        transient = isinstance(error, SpaceConnectionError) or (
            isinstance(error, SpaceApiError) and failure is None and error.status_code >= 500
        )
        if not (transient or isinstance(failure, IdempotencyInProgressError)):
            return None
        retry_after = (
            parse_retry_after(error.headers.get("retry-after")) if isinstance(error, SpaceApiError) else None
        )
        return self._space_client._retry_delay(attempt, retry_after)

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

    def _supports(self, capability: str) -> bool:
        client = self._space_client
        if client._cached_capabilities() is None:
            client._probe_capabilities()
        return client._capability_in_cache(capability)

    def _run_one(self, operation: Operation[T]) -> T:
        cached = self._cached(operation)
        if cached is not None:
            return cached

        self._check_key(operation)
        for capability, what in self._preconditions(operation):
            if not self._supports(capability):
                raise self._unsupported(capability, what)

        client = self._space_client
        attempt = 0
        while True:
            try:
                answer = self._send_one(operation)
            except (SpaceApiError, SpaceConnectionError) as error:
                failure = self._idempotency_failure(operation, error)
                delay = self._next_delay(operation, error, failure, attempt)
                if delay is not None and self._supports(IDEMPOTENCY_KEY):
                    client._sleep(delay)
                    attempt += 1
                    continue
                if failure is not None:
                    raise failure from error
                if isinstance(error, SpaceApiError):
                    self._refused(operation, error)
                raise
            return operation.parse(self._cache, answer)

    def _send_one(self, operation: Operation[Any]) -> Any:
        client = self._space_client
        headers = self._headers_of(operation)
        if operation.raw:
            return client._request_raw(
                operation.method,
                operation.path,
                json=operation.json,
                headers=headers,
                accept=operation.accept,
            )
        if operation.no_content:
            return client._request_no_content(
                operation.method, operation.path, json=operation.json, headers=headers
            )
        return client._request_json(operation.method, operation.path, json=operation.json, headers=headers)


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

    async def _supports(self, capability: str) -> bool:
        client = self._space_client
        if client._cached_capabilities() is None:
            await client._probe_capabilities()
        return client._capability_in_cache(capability)

    async def _run_one(self, operation: Operation[T]) -> T:
        cached = self._cached(operation)
        if cached is not None:
            return cached

        self._check_key(operation)
        for capability, what in self._preconditions(operation):
            if not await self._supports(capability):
                raise self._unsupported(capability, what)

        client = self._space_client
        attempt = 0
        while True:
            try:
                answer = await self._send_one(operation)
            except (SpaceApiError, SpaceConnectionError) as error:
                failure = self._idempotency_failure(operation, error)
                delay = self._next_delay(operation, error, failure, attempt)
                if delay is not None and await self._supports(IDEMPOTENCY_KEY):
                    await client._sleep(delay)
                    attempt += 1
                    continue
                if failure is not None:
                    raise failure from error
                if isinstance(error, SpaceApiError):
                    self._refused(operation, error)
                raise
            return operation.parse(self._cache, answer)

    async def _send_one(self, operation: Operation[Any]) -> Any:
        client = self._space_client
        headers = self._headers_of(operation)
        if operation.raw:
            return await client._request_raw(
                operation.method,
                operation.path,
                json=operation.json,
                headers=headers,
                accept=operation.accept,
            )
        if operation.no_content:
            return await client._request_no_content(
                operation.method, operation.path, json=operation.json, headers=headers
            )
        return await client._request_json(
            operation.method, operation.path, json=operation.json, headers=headers
        )
