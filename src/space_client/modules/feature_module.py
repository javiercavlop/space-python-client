from __future__ import annotations

import warnings
from typing import Any

from .. import _operations as operations
from .._result import Result
from ..types.models import FeatureEvaluationResult
from ._runner import AsyncRunner, SyncRunner


class _FeatureCalls:
    """The feature calls, written once.

    Each method says what to do and hands it to ``self._run``, which is the only
    part that differs between the synchronous and asynchronous clients. On
    :class:`FeatureModule` these methods return their value; on
    :class:`AsyncFeatureModule` they return an awaitable of that value.
    """

    _run: Any

    def evaluate(
        self,
        user_id: str,
        feature_id: str,
        expected_consumption: dict[str, int | float] | None = None,
        details: bool = False,
        server: bool = False,
        idempotency_key: str | None = None,
    ) -> Result[FeatureEvaluationResult]:
        """Evaluate a feature for a user.

        With an expected consumption this is a write: Space consumes when the
        feature is enabled and the amounts fit the limits (``atomic-usage-limits``),
        and the result carries ``consumption_id`` for :meth:`revert_consumption`.
        The request carries an ``Idempotency-Key`` and is retried with it per
        the client's ``retries``, so a retried consumption is applied once.

        Args:
            user_id (str): User identifier.
            feature_id (str): Feature identifier, usually `service-feature`.
            expected_consumption (dict[str, int | float] | None): Optional consumption
                projection used by Space for optimistic updates.
            details (bool): Whether to request detailed evaluation information.
            server (bool): Whether to force server-side evaluation mode.
            idempotency_key (str | None): ``Idempotency-Key`` of a consumption
                (ignored without one). Default: a new UUID v4.

        Returns:
            FeatureEvaluationResult: Evaluation outcome, including optional error payload.

        Raises:
            IdempotencyConflictError, IdempotencyInProgressError,
            IdempotencyKeyInvalidError: Space refused the key.
        """
        return self._run(
            operations.evaluate(user_id, feature_id, expected_consumption, details, server, idempotency_key)
        )

    def revert_consumption(
        self,
        user_id: str,
        feature_id: str,
        consumption_id: str,
        idempotency_key: str | None = None,
    ) -> Result[bool]:
        """Revert exactly the consumption an evaluation reported as ``consumption_id``.

        Its amounts are subtracted once, never below zero, and nothing recorded
        since is lost; reverting it again changes nothing. Needs a Space with
        ``atomic-usage-limits`` (an older one ignores the id and restores a
        cached snapshot of the level): without it this raises
        :class:`~space_client.errors.SpaceFeatureUnsupportedError` and sends
        nothing. Sent with an ``Idempotency-Key`` and retried like the other
        keyed writes.

        Args:
            user_id (str): User identifier.
            feature_id (str): Feature the consumption was made for.
            consumption_id (str): ``consumption_id`` of the evaluation.
            idempotency_key (str | None): ``Idempotency-Key`` of this revert.

        Returns:
            bool: True when the consumption is reverted (now or before); False
            when Space does not know it (404: unknown or expired id, another
            user or feature, no contract) - nothing changed.

        Raises:
            ValueError: An empty ``consumption_id``.
            SpaceFeatureUnsupportedError: Space lacks ``atomic-usage-limits``.
        """
        return self._run(operations.revert_consumption(user_id, feature_id, consumption_id, idempotency_key))

    def revert_evaluation(
        self,
        user_id: str,
        feature_id: str,
        revert_to_latest: bool = True,
        idempotency_key: str | None = None,
    ) -> Result[bool]:
        """Revert usage changes done by a previous optimistic evaluation.

        .. deprecated::
            Use :meth:`revert_consumption` with the ``consumption_id`` of the
            evaluation. This reverts whichever consumption of the feature Space
            finds in the last two minutes (the oldest, or the newest with
            ``revert_to_latest``), which may not be the one meant.

        Args:
            user_id (str): User identifier.
            feature_id (str): Feature identifier.
            revert_to_latest (bool): True to restore latest value, False for earliest.
            idempotency_key (str | None): ``Idempotency-Key`` of this revert.

        Returns:
            bool: True when the revert request succeeds, otherwise False.
        """
        warnings.warn(
            "revert_evaluation is deprecated; use revert_consumption(user_id, feature_id, consumption_id)",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._run(operations.revert_evaluation(user_id, feature_id, revert_to_latest, idempotency_key))

    def generate_user_pricing_token(self, user_id: str) -> Result[str | None]:
        """Generate a pricing token for a user.

        Args:
            user_id (str): User identifier.

        Returns:
            str | None: Token string on success, otherwise None.
        """
        return self._run(operations.generate_user_pricing_token(user_id))

    # Java compatibility aliases
    revertEvaluation = revert_evaluation
    revertConsumption = revert_consumption
    generateUserPricingToken = generate_user_pricing_token


class FeatureModule(_FeatureCalls, SyncRunner):
    """Feature operations over the synchronous client."""


class AsyncFeatureModule(_FeatureCalls, AsyncRunner):
    """Feature operations over the asynchronous client.

    Same methods and same arguments as :class:`FeatureModule`; every one of them
    must be awaited.
    """
