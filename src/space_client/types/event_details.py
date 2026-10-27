"""What pricing and contract listeners receive, and the options around it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal


class EventDetails(dict):
    """The ``details`` of a Space event, with the event's own fields as attributes.

    It is the ``details`` dict Space sent (so existing listeners keep working),
    plus:

    - ``code``: the event code as sent, e.g. ``"CONTRACT_NOVATED"``.
    - ``event_id``: identifies the event; every copy of one event has the same
      one. The client already drops repeats it has seen (see
      :class:`EventDeduplicationOptions`). Compare it for equality only, never
      to order events.
    - ``occurred_at``: when Space stored the change, as an aware ``datetime``
      (UTC), or ``None`` if it is missing or not a valid date. It comes from
      the clock of the Space instance; do not order events of different
      instances by it.
    - ``raw_occurred_at``: ``occurredAt`` exactly as Space sent it.

    ``event_id`` and ``occurred_at`` are ``None`` with Space versions without
    the ``contract-event-ids`` capability.
    """

    code: str
    event_id: str | None
    occurred_at: datetime | None
    raw_occurred_at: str | None

    def __init__(
        self,
        details: dict[str, Any],
        *,
        code: str = "",
        event_id: str | None = None,
        raw_occurred_at: str | None = None,
    ) -> None:
        super().__init__(details)
        self.code = code
        self.event_id = event_id
        self.raw_occurred_at = raw_occurred_at
        self.occurred_at = parse_occurred_at(raw_occurred_at)


def parse_occurred_at(value: str | None) -> datetime | None:
    """Parse an ISO 8601 ``occurredAt`` (``...Z`` accepted on Python 3.10)."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Space sends UTC; read an offset-less value the same way.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class ResyncInfo:
    """Passed to ``resync`` listeners each time a namespace connects.

    Space does not store or replay events, so whatever changed while a
    namespace was disconnected was not announced: re-read the contracts you
    hold (comparing their ``contractVersion``) when ``reconnect`` is true.
    """

    namespace: Literal["pricings", "contracts"]
    reconnect: bool
    """``False`` on the namespace's first connection, ``True`` afterwards."""


@dataclass(frozen=True)
class EventDeduplicationOptions:
    """How the client drops repeated events by ``eventId``."""

    enabled: bool = True
    """``False`` delivers every copy."""
    max_entries: int = 10_000
    """At most this many ids are remembered; the oldest go first."""
    window_seconds: float = 600
    """An id is forgotten this long after it was first seen."""
