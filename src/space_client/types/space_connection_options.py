from __future__ import annotations

from dataclasses import dataclass

from .cache_options import CacheOptions
from .event_details import EventDeduplicationOptions


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
    event_deduplication: EventDeduplicationOptions | bool = True
    """Drop repeated events by ``eventId`` (a consumer can receive one event
    more than once). ``True`` (the default) remembers the last 10,000 ids for
    up to 10 minutes; pass :class:`EventDeduplicationOptions` to change that,
    or ``False`` to deliver every copy. Events without ``eventId`` (older
    Space) are always delivered."""
