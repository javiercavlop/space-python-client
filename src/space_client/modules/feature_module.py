from __future__ import annotations

from typing import Any

from .. import _operations as operations
from .._result import Result
from ..pricing_token_verifier import AsyncPricingTokenVerifier, PricingTokenVerifier
from ..types.models import FeatureEvaluationResult
from ..types.pricing_token import PricingTokenPayload
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
    ) -> Result[FeatureEvaluationResult]:
        """Evaluate a feature for a user.

        Args:
            user_id (str): User identifier.
            feature_id (str): Feature identifier, usually `service-feature`.
            expected_consumption (dict[str, int | float] | None): Optional consumption
                projection used by Space for optimistic updates.
            details (bool): Whether to request detailed evaluation information.
            server (bool): Whether to force server-side evaluation mode.

        Returns:
            FeatureEvaluationResult: Evaluation outcome, including optional error payload.
        """
        return self._run(
            operations.evaluate(user_id, feature_id, expected_consumption, details, server)
        )

    def revert_evaluation(
        self, user_id: str, feature_id: str, revert_to_latest: bool = True
    ) -> Result[bool]:
        """Revert usage changes done by a previous optimistic evaluation.

        Args:
            user_id (str): User identifier.
            feature_id (str): Feature identifier.
            revert_to_latest (bool): True to restore latest value, False for earliest.

        Returns:
            bool: True when the revert request succeeds, otherwise False.
        """
        return self._run(operations.revert_evaluation(user_id, feature_id, revert_to_latest))

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
    generateUserPricingToken = generate_user_pricing_token


_VERIFY_DOC = """Verify a pricing token with the public keys of this Space.

        Checks the ES256 signature by a key of the JWKS Space publishes
        (capability ``pricing-token-jwks``), ``exp``, ``iss`` and
        ``aud``/``organizationId``, and returns the payload. Use it for tokens that
        come back from a browser or another service; no API key is sent. The JWKS
        is cached by the client (see :class:`~space_client.PricingTokenVerifier`).

        Args:
            token (str): The pricing token.
            organization_id (str | None): The organization the token must be
                issued for. Defaults to the ``organization_id`` of the client; one
                of the two is required.
            issuer (str | None): The expected ``iss``. Default ``space``.
            leeway (float | None): Tolerance for ``exp``, in seconds. Default 0.

        Returns:
            PricingTokenPayload: The verified claims.

        Raises:
            ValueError: Without an organization.
            PricingTokenVerificationError: Or a subclass, when the token cannot be trusted.
            SpaceFeatureUnsupportedError: When Space does not publish its keys.
        """


class FeatureModule(_FeatureCalls, SyncRunner):
    """Feature operations over the synchronous client."""

    _verifier: PricingTokenVerifier | None = None

    def verify_pricing_token(
        self,
        token: str,
        organization_id: str | None = None,
        issuer: str | None = None,
        leeway: float | None = None,
    ) -> PricingTokenPayload:
        if self._verifier is None:
            client = self._space_client
            self._verifier = PricingTokenVerifier(
                jwks_url=f"{client.get_http_url()}/.well-known/jwks.json",
                http_client=client._http_client,
                timeout=client.get_timeout() / 1000,
            )
        return self._verifier.verify(
            token,
            organization_id=organization_id or self._space_client._organization_id,
            issuer=issuer,
            leeway=leeway,
        )

    verify_pricing_token.__doc__ = _VERIFY_DOC
    verifyPricingToken = verify_pricing_token


class AsyncFeatureModule(_FeatureCalls, AsyncRunner):
    """Feature operations over the asynchronous client.

    Same methods and same arguments as :class:`FeatureModule`; every one of them
    must be awaited.
    """

    _verifier: AsyncPricingTokenVerifier | None = None

    async def verify_pricing_token(
        self,
        token: str,
        organization_id: str | None = None,
        issuer: str | None = None,
        leeway: float | None = None,
    ) -> PricingTokenPayload:
        if self._verifier is None:
            client = self._space_client
            self._verifier = AsyncPricingTokenVerifier(
                jwks_url=f"{client.get_http_url()}/.well-known/jwks.json",
                http_client=client._http_client,
                timeout=client.get_timeout() / 1000,
            )
        return await self._verifier.verify(
            token,
            organization_id=organization_id or self._space_client._organization_id,
            issuer=issuer,
            leeway=leeway,
        )

    verify_pricing_token.__doc__ = _VERIFY_DOC
    verifyPricingToken = verify_pricing_token
