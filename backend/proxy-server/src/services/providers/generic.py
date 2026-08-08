from typing import Any

import httpx

from src.models import APIProvider
from .base import BaseProvider


class GenericProvider(BaseProvider):
    HTTP_METHOD = "GET"
    async def execute(
        self,
        provider: APIProvider,
        api_key: str,
        params: dict[str, Any],
        **kwargs,
    ) -> dict[str, Any]:

        headers = {}
        query_params = dict(params)

        if provider.auth_type == "api_key":

            if (
                provider.auth_header.startswith("X-")
                or provider.auth_header == "Authorization"
            ):
                headers[provider.auth_header] = api_key

            else:
                query_params[provider.auth_header] = api_key

        elif provider.auth_type == "bearer":

            headers["Authorization"] = f"Bearer {api_key}"

        async with httpx.AsyncClient(timeout=30) as client:

            response = await client.get(
                provider.base_url,
                params=query_params,
                headers=headers,
            )

            response.raise_for_status()

            return response.json()