from __future__ import annotations

from dataclasses import dataclass

from .cache_options import CacheOptions


@dataclass
class SpaceConnectionOptions:
    url: str | None
    api_key: str | None
    timeout: int = 5000
    cache: CacheOptions | None = None
    organization_id: str | None = None
    """Organisation of a member user key (``usr_...``). Sent with the
    ``/contracts`` events handshake, which Space requires for user keys; not
    needed for organisation keys."""
    retries: int = 0
    """How many times a write carrying an ``Idempotency-Key`` (consumptions,
    reverts, contract creation, novations, usage level changes) is retried,
    with the same key, after a network error, a ``5xx`` or ``409
    IDEMPOTENCY_IN_PROGRESS``. 0 to 10; default 0 (no retries). Retries only
    happen while Space advertises ``idempotency-key``: an older Space would
    apply each attempt. Other requests are never retried."""
    retry_base_delay_ms: int = 250
    """Base of the exponential backoff: the n-th retry waits between half and
    all of ``retry_base_delay_ms * 2**(n-1)``, capped at ``retry_max_delay_ms``.
    A ``Retry-After`` from Space is waited exactly instead."""
    retry_max_delay_ms: int = 10_000
    """Longest wait before a retry. A longer ``Retry-After`` ends the retries:
    the client never waits less than Space asks."""
