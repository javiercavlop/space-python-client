from enum import Enum


class SpaceEvent(str, Enum):
    SYNCHRONIZED = "synchronized"
    PRICING_CREATED = "pricing_created"
    PRICING_ARCHIVED = "pricing_archived"
    PRICING_ACTIVED = "pricing_actived"
    SERVICE_DISABLED = "service_disabled"
    CONTRACT_CREATED = "contract_created"
    CONTRACT_NOVATED = "contract_novated"
    CONTRACT_DELETED = "contract_deleted"
    CONTRACT_RENEWED = "contract_renewed"
    ERROR = "error"

    @classmethod
    def from_string(cls, text: str) -> "SpaceEvent | None":
        """Resolve enum value from string.

        Args:
            text (str): Event name in any case.

        Returns:
            SpaceEvent | None: Matching enum value, or None if text is unknown.
        """
        lowered = text.lower()
        for event in cls:
            if event.value == lowered:
                return event
        return None
