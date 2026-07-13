"""
Secrets Service — retrieves provider API keys from AWS Secrets Manager.

In production: all keys live in AWS Secrets Manager. Never in env vars or code.
In development: falls back to DEV_*_KEY environment variables for local testing.

Secret naming convention in AWS: plexus/providers/<slug>
e.g. plexus/providers/serpapi  →  {"api_key": "abc123"}
"""

import json
from functools import lru_cache
from typing import Optional

import boto3
import structlog
from botocore.exceptions import ClientError

from src.config import settings

log = structlog.get_logger()

# Local dev fallback map: provider slug → env var name
DEV_KEY_MAP = {
    "serpapi":      settings.DEV_SERPAPI_KEY,
    "apify":        settings.DEV_APIFY_KEY,
    "openweather":  settings.DEV_OPENWEATHER_KEY,
}


class SecretsService:
    def __init__(self):
        if settings.use_aws_secrets:
            self._client = boto3.client(
                "secretsmanager",
                region_name=settings.AWS_REGION,
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            )
        else:
            self._client = None
        # Simple in-process cache — avoids hammering AWS on every request.
        # In production, use a short TTL cache (5 min) with Redis for multi-instance.
        self._cache: dict[str, str] = {}

    async def get_provider_key(self, provider_slug: str) -> str:
        """
        Retrieve the API key for a provider.
        Returns a string key. Raises KeyError if not found.
        """
        # Check cache first
        if provider_slug in self._cache:
            return self._cache[provider_slug]

        if settings.use_aws_secrets:
            key = await self._fetch_from_aws(provider_slug)
        else:
            key = self._fetch_from_env(provider_slug)

        self._cache[provider_slug] = key
        return key

    async def _fetch_from_aws(self, provider_slug: str) -> str:
        secret_name = f"plexus/providers/{provider_slug}"
        try:
            response = self._client.get_secret_value(SecretId=secret_name)
            secret = json.loads(response["SecretString"])
            log.info("secrets.fetched", provider=provider_slug)
            return secret["api_key"]
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                raise KeyError(f"No secret found for provider: {provider_slug}")
            raise

    def _fetch_from_env(self, provider_slug: str) -> str:
        """Dev fallback — reads from DEV_*_KEY env vars."""
        key = DEV_KEY_MAP.get(provider_slug, "")
        if not key:
            raise KeyError(
                f"No dev key for '{provider_slug}'. "
                f"Set DEV_{provider_slug.upper()}_KEY in your .env file."
            )
        log.debug("secrets.dev_fallback", provider=provider_slug)
        return key

    def invalidate_cache(self, provider_slug: Optional[str] = None):
        """Clear cache — call when a provider key is rotated."""
        if provider_slug:
            self._cache.pop(provider_slug, None)
        else:
            self._cache.clear()


# Singleton — one instance shared across the app
secrets_service = SecretsService()