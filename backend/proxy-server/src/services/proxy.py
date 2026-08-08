"""
Proxy Service — the core of Plexus.

This is what happens on every agent API call:
  1. Look up the provider in the registry
  2. Debit-lock credits (two-phase commit, phase 1)
  3. Fetch the provider's API key from secrets vault
  4. Make the actual HTTP request to the provider
  5. Settle (success) or refund (failure) the credit lock
  6. Return the provider's response to the agent

The agent never sees the provider's API key. It only ever talks to Plexus.
"""

"""
Proxy Service — the core of Plexus.

This is what happens on every agent API call:
  1. Look up the provider in the registry
  2. Debit-lock credits (two-phase commit, phase 1)
  3. Fetch the provider's API key from secrets vault
  4. Make the actual HTTP request to the provider
  5. Settle (success) or refund (failure) the credit lock
  6. Return the provider's response to the agent

The agent never sees the provider's API key. It only ever talks to Plexus.
"""

import time
import uuid
from typing import Any

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import APIProvider, APICall, Developer
from src.services.wallet import WalletService, InsufficientCreditsError
from src.services.secrets import secrets_service
from src.services.providers.factory import ProviderFactory
from src.config import settings

log = structlog.get_logger()

# Timeout for provider API calls (seconds)
PROVIDER_TIMEOUT = 30.0


class ProviderError(Exception):
    """The upstream provider returned an error."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message


class ProxyService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.wallet = WalletService(db)

    async def call(
        self,
        developer,
        provider,
        sdk_token_id,
        params,
        idempotency_key,
        actor: str | None = None,
    ):
        """
        Route an agent's API call through the Plexus proxy.
        Returns the provider's JSON response.
        """

        call_id = uuid.uuid4()
        start_ms = int(time.time() * 1000)

        # Get the correct provider adapter
        adapter = ProviderFactory.get(provider.slug)

        # Step 1: Log the call attempt
        api_call = APICall(
            id=call_id,
            developer_id=developer.id,
            provider_id=provider.id,
            sdk_token_id=sdk_token_id,
            endpoint=provider.base_url,
            method=adapter.HTTP_METHOD,
            cost_credits=provider.cost_per_call,
        )

        self.db.add(api_call)
        await self.db.flush()

        # Step 2: Lock credits
        tx = await self.wallet.debit_lock(
            developer_id=developer.id,
            amount_credits=provider.cost_per_call,
            idempotency_key=idempotency_key,
            api_call_id=call_id,
        )

        # Step 3: Retrieve provider API key
        try:
            api_key = await secrets_service.get_provider_key(provider.slug)
        except KeyError:
            await self.wallet.debit_refund(tx.id)
            raise ProviderError(
                503,
                f"Provider not configured: {provider.slug}",
            )

        # Step 4: Execute provider request
        try:
            result = await adapter.execute(
                provider=provider,
                api_key=api_key,
                params=params,
                actor=actor,
            )

            latency_ms = int(time.time() * 1000) - start_ms

            # Step 5a: Settle credits
            await self.wallet.debit_settle(tx.id)

            api_call.status_code = 200
            api_call.latency_ms = latency_ms

            await self.db.flush()

            log.info(
                "proxy.call.success",
                provider=provider.slug,
                developer_id=str(developer.id),
                latency_ms=latency_ms,
                credits=provider.cost_per_call,
            )

            return result

        except (httpx.HTTPStatusError, ProviderError) as e:

            await self.wallet.debit_refund(tx.id)

            status = (
                e.response.status_code
                if hasattr(e, "response")
                else e.status_code
                if hasattr(e, "status_code")
                else 502
            )

            api_call.status_code = status
            api_call.error = str(e)
            api_call.latency_ms = (
                int(time.time() * 1000) - start_ms
            )

            await self.db.flush()

            log.error(
                "proxy.call.provider_error",
                provider=provider.slug,
                error=str(e),
            )

            raise ProviderError(status, str(e))

        except Exception as e:

            await self.wallet.debit_refund(tx.id)

            api_call.status_code = 500
            api_call.error = str(e)
            api_call.latency_ms = (
                int(time.time() * 1000) - start_ms
            )

            await self.db.flush()

            log.error(
                "proxy.call.unexpected_error",
                provider=provider.slug,
                error=str(e),
            )

            raise

    # async def _call_provider(
    #     self,
    #     provider: APIProvider,
    #     api_key: str,
    #     params: dict[str, Any],
    # ) -> dict[str, Any]:
    #     """
    #     Make the actual HTTP call to the provider.
    #     Auth injection is handled here — the agent never sees the key.

    #     Currently supports: api_key (query param or header), bearer token.
    #     Production: parse provider's OpenAPI spec for exact auth format.
    #     """
    #     headers = {}
    #     query_params = dict(params)  # copy so we don't mutate caller's dict

    #     if provider.auth_type == "api_key":
    #         if provider.auth_header.startswith("X-") or provider.auth_header == "Authorization":
    #             # Header-based auth
    #             headers[provider.auth_header] = api_key
    #         else:
    #             # Query param auth (e.g. SerpAPI uses ?api_key=...)
    #             query_params[provider.auth_header] = api_key

    #     elif provider.auth_type == "bearer":
    #         headers["Authorization"] = f"Bearer {api_key}"

    #     async with httpx.AsyncClient(timeout=PROVIDER_TIMEOUT) as client:
    #         response = await client.get(
    #             provider.base_url,
    #             params=query_params,
    #             headers=headers,
    #         )
    #         response.raise_for_status()
    #         return response.json()