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
