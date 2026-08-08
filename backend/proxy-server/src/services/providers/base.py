from abc import ABC, abstractmethod
from typing import Any

from src.models import APIProvider


class BaseProvider(ABC):
    HTTP_METHOD = "GET"
    @abstractmethod
    async def execute(
        self,
        provider: APIProvider,
        api_key: str,
        params: dict[str, Any],
        **kwargs,
    ) -> dict[str, Any]:
        """
        Execute a provider-specific API call.

        kwargs contains provider-specific metadata
        (e.g. actor for Apify).
        """
        pass