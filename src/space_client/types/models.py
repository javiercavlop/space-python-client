from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class EvaluationError:
    code: str
    message: str

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EvaluationError | None":
        """Build an error model from payload.

        Args:
            data (dict[str, Any] | None): Raw API error object.

        Returns:
            EvaluationError | None: Parsed error model or None when input is None.
        """
        if data is None:
            return None
        return cls(code=str(data.get("code", "")), message=str(data.get("message", "")))


@dataclass
class FeatureEvaluationResult:
    eval: bool = False
    used: dict[str, Any] = field(default_factory=dict)
    limit: dict[str, Any] = field(default_factory=dict)
    error: EvaluationError | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FeatureEvaluationResult":
        """Build evaluation result from payload.

        Args:
            data (dict[str, Any] | None): Raw API response object.

        Returns:
            FeatureEvaluationResult: Parsed evaluation result.
        """
        payload = data or {}
        return cls(
            eval=bool(payload.get("eval", False)),
            used=dict(payload.get("used", {})),
            limit=dict(payload.get("limit", {})),
            error=EvaluationError.from_dict(payload.get("error")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert model to payload dictionary.

        Returns:
            dict[str, Any]: JSON-serializable representation.
        """
        result = asdict(self)
        if self.error is None:
            result["error"] = None
        return result


@dataclass
class UserContact:
    user_id: str
    username: str
    first_name: str = ""
    last_name: str = ""
    email: str | None = None
    phone: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "UserContact | None":
        """Build user contact from payload.

        Args:
            data (dict[str, Any] | None): Raw contact payload.

        Returns:
            UserContact | None: Parsed model or None when input is None.
        """
        if data is None:
            return None
        return cls(
            user_id=str(data.get("userId", "")),
            username=str(data.get("username", "")),
            first_name=str(data.get("firstName", "")),
            last_name=str(data.get("lastName", "")),
            email=data.get("email"),
            phone=data.get("phone"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert model to payload dictionary.

        Optional fields that were never set are omitted rather than sent as
        JSON null. Space marks them optional in a way that accepts a missing
        key and refuses an explicit null - ``{"phone": null}`` comes back as
        ``422 The userContact.phone field must be a string`` - so serialising
        an unset field makes the dataclass default unusable.

        Returns:
            dict[str, Any]: API-compatible contact object.
        """
        payload: dict[str, Any] = {
            "userId": self.user_id,
            "username": self.username,
            "firstName": self.first_name,
            "lastName": self.last_name,
        }
        if self.email is not None:
            payload["email"] = self.email
        if self.phone is not None:
            payload["phone"] = self.phone
        return payload


@dataclass
class BillingPeriod:
    start_date: str | None = None
    end_date: str | None = None
    auto_renew: bool = False
    renewal_days: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "BillingPeriod | None":
        """Build billing period from payload.

        Args:
            data (dict[str, Any] | None): Raw billing period payload.

        Returns:
            BillingPeriod | None: Parsed model or None when input is None.
        """
        if data is None:
            return None
        return cls(
            start_date=data.get("startDate"),
            end_date=data.get("endDate"),
            auto_renew=bool(data.get("autoRenew", False)),
            renewal_days=int(data.get("renewalDays", 0)),
        )


@dataclass
class BillingPeriodToCreate:
    auto_renew: bool | None = None
    renewal_days: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert model to payload dictionary.

        Returns:
            dict[str, Any]: API-compatible creation payload.
        """
        return {
            "autoRenew": self.auto_renew,
            "renewalDays": self.renewal_days,
        }


@dataclass
class UsageLevel:
    reset_time_stamp: str | None = None
    consumed: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | int | float | None) -> "UsageLevel":
        """Build usage level from payload.

        Args:
            data (dict[str, Any] | int | float | None): Raw usage object or scalar.

        Returns:
            UsageLevel: Parsed usage model.
        """
        if isinstance(data, (int, float)):
            return cls(reset_time_stamp=None, consumed=float(data))
        payload = data or {}
        return cls(
            reset_time_stamp=payload.get("resetTimeStamp"),
            consumed=float(payload.get("consumed", 0.0)),
        )


@dataclass
class ContractHistoryEntry:
    start_date: str | None = None
    end_date: str | None = None
    contracted_services: dict[str, str] = field(default_factory=dict)
    subscription_plans: dict[str, str] = field(default_factory=dict)
    subscription_add_ons: dict[str, dict[str, int]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ContractHistoryEntry":
        """Build contract history entry from payload.

        Args:
            data (dict[str, Any] | None): Raw history payload.

        Returns:
            ContractHistoryEntry: Parsed history model.
        """
        payload = data or {}
        return cls(
            start_date=payload.get("startDate"),
            end_date=payload.get("endDate"),
            contracted_services=dict(payload.get("contractedServices", {})),
            subscription_plans=dict(payload.get("subscriptionPlans", {})),
            subscription_add_ons=dict(payload.get("subscriptionAddOns", {})),
        )


@dataclass
class Contract:
    id: str | None = None
    mongo_id: str | None = None
    user_contact: UserContact | None = None
    billing_period: BillingPeriod | None = None
    organization_id: str | None = None
    group_id: str | None = None
    usage_levels: dict[str, dict[str, UsageLevel]] = field(default_factory=dict)
    contracted_services: dict[str, str] = field(default_factory=dict)
    subscription_plans: dict[str, str] = field(default_factory=dict)
    subscription_add_ons: dict[str, dict[str, int]] = field(default_factory=dict)
    history: list[ContractHistoryEntry] = field(default_factory=list)

    @property
    def user_id(self) -> str | None:
        """Get user identifier from nested contact.

        Returns:
            str | None: User identifier, or None when contact is unavailable.
        """
        return self.user_contact.user_id if self.user_contact else None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Contract":
        """Build contract model from payload.

        Args:
            data (dict[str, Any] | None): Raw contract payload.

        Returns:
            Contract: Parsed contract model.
        """
        payload = data or {}
        usage_levels_raw = payload.get("usageLevels", {})
        usage_levels: dict[str, dict[str, UsageLevel]] = {}
        for service_name, feature_levels in usage_levels_raw.items():
            usage_levels[service_name] = {
                feature_name: UsageLevel.from_dict(level)
                for feature_name, level in dict(feature_levels).items()
            }

        history_raw = payload.get("history", [])
        return cls(
            id=payload.get("id"),
            mongo_id=payload.get("_id"),
            user_contact=UserContact.from_dict(payload.get("userContact")),
            billing_period=BillingPeriod.from_dict(payload.get("billingPeriod")),
            organization_id=payload.get("organizationId"),
            group_id=payload.get("groupId"),
            usage_levels=usage_levels,
            contracted_services=dict(payload.get("contractedServices", {})),
            subscription_plans=dict(payload.get("subscriptionPlans", {})),
            subscription_add_ons=dict(payload.get("subscriptionAddOns", {})),
            history=[ContractHistoryEntry.from_dict(item) for item in history_raw],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert model to payload dictionary.

        Returns:
            dict[str, Any]: JSON-serializable contract object.
        """
        return {
            "id": self.id,
            "_id": self.mongo_id,
            "userContact": self.user_contact.to_dict() if self.user_contact else None,
            "billingPeriod": asdict(self.billing_period) if self.billing_period else None,
            "organizationId": self.organization_id,
            "groupId": self.group_id,
            "usageLevels": {
                service_name: {
                    feature_name: asdict(level)
                    for feature_name, level in features.items()
                }
                for service_name, features in self.usage_levels.items()
            },
            "contractedServices": self.contracted_services,
            "subscriptionPlans": self.subscription_plans,
            "subscriptionAddOns": self.subscription_add_ons,
            "history": [asdict(entry) for entry in self.history],
        }


@dataclass
class ContractToCreate:
    user_contact: UserContact
    billing_period: BillingPeriodToCreate
    contracted_services: dict[str, str]
    group_id: str | None = None
    subscription_plans: dict[str, str] = field(default_factory=dict)
    subscription_add_ons: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert model to payload dictionary.

        Returns:
            dict[str, Any]: API-compatible contract creation payload.
        """
        return {
            "userContact": self.user_contact.to_dict(),
            "billingPeriod": self.billing_period.to_dict(),
            "contractedServices": self.contracted_services,
            "groupId": self.group_id,
            "subscriptionPlans": self.subscription_plans,
            "subscriptionAddOns": self.subscription_add_ons,
        }


@dataclass
class Subscription:
    contracted_services: dict[str, str] = field(default_factory=dict)
    subscription_plans: dict[str, str] = field(default_factory=dict)
    subscription_add_ons: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert model to payload dictionary.

        Returns:
            dict[str, Any]: API-compatible subscription update payload.
        """
        return {
            "contractedServices": self.contracted_services,
            "subscriptionPlans": self.subscription_plans,
            "subscriptionAddOns": self.subscription_add_ons,
        }


@dataclass
class PricingYaml:
    """The raw Pricing2Yaml file of a pricing version, as Space stores it.

    Attributes:
        yaml: The file, byte for byte (decoded as UTF-8). ``None`` when
            ``not_modified`` is True.
        etag: The strong ETag of the file (``"<sha256 hex>"``), or None.
        syntax_version: The file's ``syntaxVersion`` (header
            ``X-Pricing-Syntax-Version``), or None.
        not_modified: True when the ``etag`` passed in still matches (HTTP 304).
    """

    yaml: str | None
    etag: str | None = None
    syntax_version: str | None = None
    not_modified: bool = False


@dataclass
class ContractServicePricing:
    """The pricing version a contract holds for one of its services."""

    version: str
    status: str
    yaml_url: str
    new_contracts_version: str | None = None
    plan: str | None = None
    add_ons: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ContractServicePricing":
        payload = data or {}
        return cls(
            version=str(payload.get("version", "")),
            status=str(payload.get("status", "")),
            yaml_url=str(payload.get("yamlUrl", "")),
            new_contracts_version=payload.get("newContractsVersion"),
            plan=payload.get("plan"),
            add_ons=dict(payload.get("addOns") or {}),
        )


@dataclass
class ContractPricings:
    """Answer of ``GET /contracts/{userId}/pricings``.

    ``contract_version`` is opaque; pass it as ``expected_contract_version``
    when applying a change computed from this answer.
    """

    services: dict[str, ContractServicePricing] = field(default_factory=dict)
    contract_version: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ContractPricings":
        payload = data or {}
        return cls(
            services={
                name: ContractServicePricing.from_dict(item)
                for name, item in dict(payload.get("services") or {}).items()
            },
            contract_version=payload.get("contractVersion"),
        )


@dataclass
class NewContractsPricing:
    """Answer of ``GET /services/{s}/pricings/new-contracts``.

    ``source`` is ``"sphere-policy"``, ``"default"`` or ``"latest-active"``.
    """

    version: str
    yaml_url: str
    source: str

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "NewContractsPricing":
        payload = data or {}
        return cls(
            version=str(payload.get("version", "")),
            yaml_url=str(payload.get("yamlUrl", "")),
            source=str(payload.get("source", "")),
        )


__all__ = [
    "BillingPeriod",
    "BillingPeriodToCreate",
    "Contract",
    "ContractHistoryEntry",
    "ContractPricings",
    "ContractServicePricing",
    "ContractToCreate",
    "EvaluationError",
    "FeatureEvaluationResult",
    "NewContractsPricing",
    "PricingYaml",
    "Subscription",
    "UsageLevel",
    "UserContact",
]
