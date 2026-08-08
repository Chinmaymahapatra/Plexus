import asyncio
from typing import Any

import httpx

from src.models import APIProvider
from .base import BaseProvider


class ApifyProvider(BaseProvider):
    HTTP_METHOD = "POST"

    BASE_URL = "https://api.apify.com/v2"

    POLL_INTERVAL = 2

    MAX_POLLS = 120

    async def execute(
        self,
        provider: APIProvider,
        api_key: str,
        params: dict[str, Any],
        **kwargs,
    ) -> dict[str, Any]:

        actor = kwargs.get("actor")

        if not actor:
            raise ValueError("Missing Apify actor.")

        async with httpx.AsyncClient(timeout=60) as client:

            run = await self.start_actor(
                client,
                actor,
                api_key,
                params,
            )

            return await self.wait_for_completion(
                client,
                run,
                api_key,
            )

    async def start_actor(
        self,
        client: httpx.AsyncClient,
        actor: str,
        api_key: str,
        input_data: dict[str, Any],
    ) -> dict[str, Any]:

        actor = actor.replace("/", "~")

        url = f"{self.BASE_URL}/acts/{actor}/runs"

        response = await client.post(
            url,
            params={
                "token": api_key,
            },
            json=input_data,
        )

        print("\n================ APIFY DEBUG ================")
        print("URL:", response.request.url)
        print("STATUS:", response.status_code)
        print("BODY:", response.text)
        print("=============================================\n")

        response.raise_for_status()

        return response.json()["data"]

    async def wait_for_completion(
        self,
        client: httpx.AsyncClient,
        run: dict[str, Any],
        api_key: str,
    ) -> dict[str, Any]:

        run_id = run["id"]

        for _ in range(self.MAX_POLLS):

            await asyncio.sleep(self.POLL_INTERVAL)

            response = await client.get(
                f"{self.BASE_URL}/actor-runs/{run_id}",
                params={
                    "token": api_key,
                },
            )

            response.raise_for_status()

            data = response.json()["data"]

            status = data["status"]

            if status == "SUCCEEDED":

                dataset_id = data["defaultDatasetId"]

                items = await self.fetch_dataset(
                    client,
                    dataset_id,
                    api_key,
                )

                return {
                    "run_id": run_id,
                    "dataset_id": dataset_id,
                    "status": status,
                    "items": items,
                }

            if status in (
                "FAILED",
                "ABORTED",
                "TIMED-OUT",
            ):
                raise RuntimeError(f"Actor failed ({status})")

        raise TimeoutError("Actor timed out.")

    async def fetch_dataset(
        self,
        client: httpx.AsyncClient,
        dataset_id: str,
        api_key: str,
    ) -> list[dict[str, Any]]:

        response = await client.get(
            f"{self.BASE_URL}/datasets/{dataset_id}/items",
            params={
                "token": api_key,
            },
        )

        response.raise_for_status()

        return response.json()