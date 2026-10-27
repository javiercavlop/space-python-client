from __future__ import annotations

from typing import Any, Callable, TypeVar

from ..cache.cache_provider import CacheProvider
from ..cache.cache_provider_factory import CacheProviderFactory
from ..types.cache_options import CacheOptions

T = TypeVar("T")


class CacheModule:
    def __init__(self) -> None:
        """Create cache facade used by internal modules.

        Returns:
            None: Constructor initializes cache state as disabled.
        """
        self._provider: CacheProvider | None = None
        self._enabled = False
        self._key_prefix = "space-client:"

    def initialize(self, options: CacheOptions) -> None:
        """Initialize cache provider.

        Args:
            options (CacheOptions): Cache configuration.

        Returns:
            None: Provider is created or cache is disabled.
        """
        if not options.enabled:
            self._enabled = False
            self._provider = None
            return

        CacheProviderFactory.validate(options)
        self._provider = CacheProviderFactory.create(options)
        self._enabled = True

    def is_enabled(self) -> bool:
        """Check cache availability.

        Returns:
            bool: True when cache is enabled and provider is initialized.
        """
        return self._enabled and self._provider is not None

    def get(self, key: str, parser: Callable[[Any], T] | None = None) -> T | Any | None:
        """Get cached value by key.

        Args:
            key (str): Cache key without internal prefix.
            parser (Callable[[Any], T] | None): Optional converter for raw cached value.

        Returns:
            T | Any | None: Parsed value, raw value, or None if not found/invalid.
        """
        if not self.is_enabled():
            return None
        assert self._provider is not None
        value = self._provider.get(self._full_key(key))
        if value is None:
            return None
        if parser is None:
            return value
        try:
            return parser(value)
        except Exception:
            return None

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Set a cache value.

        Args:
            key (str): Cache key without internal prefix.
            value (Any): Value to store.
            ttl (int | None): Optional TTL in seconds.

        Returns:
            None: No value is returned.
        """
        if not self.is_enabled():
            return
        assert self._provider is not None
        self._provider.set(self._full_key(key), value, ttl)

    def delete(self, key: str) -> None:
        """Delete one cache key.

        Args:
            key (str): Cache key without internal prefix.

        Returns:
            None: No value is returned.
        """
        if not self.is_enabled():
            return
        assert self._provider is not None
        self._provider.delete(self._full_key(key))

    def has(self, key: str) -> bool:
        """Check key existence.

        Args:
            key (str): Cache key without internal prefix.

        Returns:
            bool: True when key exists, otherwise False.
        """
        if not self.is_enabled():
            return False
        assert self._provider is not None
        return self._provider.has(self._full_key(key))

    def clear(self) -> None:
        """Clear all cache entries.

        Returns:
            None: No value is returned.
        """
        if not self.is_enabled():
            return
        assert self._provider is not None
        self._provider.clear()

    def keys(self, pattern: str | None = None) -> list[str]:
        """List cache keys.

        Args:
            pattern (str | None): Optional glob-style pattern.

        Returns:
            list[str]: Matching keys without internal prefix.
        """
        if not self.is_enabled():
            return []
        assert self._provider is not None
        full_pattern = f"{self._key_prefix}{pattern if pattern is not None else '*'}"
        keys = self._provider.keys(full_pattern)
        return [key.removeprefix(self._key_prefix) for key in keys]

    def get_contract_key(self, user_id: str) -> str:
        """Build user contract cache key.

        Args:
            user_id (str): User identifier.

        Returns:
            str: Canonical key for contract cache entry.
        """
        return f"contract:{user_id}"

    def get_feature_key(self, user_id: str, feature_name: str) -> str:
        """Build feature evaluation cache key.

        Args:
            user_id (str): User identifier.
            feature_name (str): Feature identifier.

        Returns:
            str: Canonical key for feature cache entry.
        """
        return f"feature:{user_id}:{feature_name}"

    def get_subscription_key(self, user_id: str) -> str:
        """Build subscription cache key.

        Args:
            user_id (str): User identifier.

        Returns:
            str: Canonical key for subscription cache entry.
        """
        return f"subscription:{user_id}"

    def get_pricing_token_key(self, user_id: str) -> str:
        """Build pricing token cache key.

        Args:
            user_id (str): User identifier.

        Returns:
            str: Canonical key for pricing token cache entry.
        """
        return f"pricing-token:{user_id}"

    def invalidate_user(self, user_id: str) -> None:
        """Invalidate all cache keys associated with one user.

        Args:
            user_id (str): User identifier.

        Returns:
            None: No value is returned.
        """
        if not self.is_enabled():
            return
        patterns = [
            f"contract:{user_id}",
            f"feature:{user_id}:*",
            f"subscription:{user_id}",
            f"pricing-token:{user_id}",
        ]
        for pattern in patterns:
            for key in self.keys(pattern):
                self.delete(key)

    def invalidate_all_users(self) -> None:
        """Invalidate the cache keys of every user.

        Contracts, evaluations, subscriptions and pricing tokens, e.g. after
        contract events may have been missed.

        Returns:
            None: No value is returned.
        """
        if not self.is_enabled():
            return
        for pattern in ("contract:*", "feature:*", "subscription:*", "pricing-token:*"):
            for key in self.keys(pattern):
                self.delete(key)

    def close(self) -> None:
        """Close provider and disable cache module.

        Returns:
            None: No value is returned.
        """
        if self._provider is not None:
            self._provider.close()
            self._provider = None
        self._enabled = False

    def _full_key(self, key: str) -> str:
        return f"{self._key_prefix}{key}"

    # Java compatibility aliases
    isEnabled = is_enabled
    getContractKey = get_contract_key
    getFeatureKey = get_feature_key
    getSubscriptionKey = get_subscription_key
    getPricingTokenKey = get_pricing_token_key
    invalidateUser = invalidate_user
