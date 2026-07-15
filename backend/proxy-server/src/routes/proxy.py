"""
POST /v1/call — the main proxy endpoint.

Request body:
  {
    "api": "serpapi",        // provider slug OR natural language intent
    "params": { "q": "AI news 2026" },
    "idempotency_key": "uuid"  // optional, generated if not provided
  }

Response: the provider's JSON response, pass-through.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models import APIProvider, Developer
from src.services.proxy import ProxyService, ProviderError
from src.services.wallet import InsufficientCreditsError

router = APIRouter()


class CallRequest(BaseModel):
    api: str = Field(..., description="Provider slug (e.g. 'serpapi') or natural language intent")
    params: dict = Field(default_factory=dict, description="Parameters to pass to the provider API")
    idempotency_key: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique key for this call. Same key returns cached result without charging again."
    )


@router.post("")
async def call_api(
    body: CallRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Route an agent's API call through the Plexus proxy.
    The agent sends a Plexus SDK token — we handle all provider auth invisibly.
    """
    developer: Developer = request.state.developer

    # Look up the provider by slug (semantic search added in Phase 2)
    result = await db.execute(
        select(APIProvider)
        .where(APIProvider.slug == body.api)
        .where(APIProvider.is_active == True)
    )
    provider = result.scalar_one_or_none()

    if not provider:
        raise HTTPException(
            status_code=404,
            detail=f"Provider '{body.api}' not found. Use GET /v1/registry to browse available APIs."
        )

    proxy = ProxyService(db)

    # CHANGED: was `sdk_token_id = uuid.uuid4()  # placeholder — wire up from auth middleware`
    # Now reads the real SDKToken row's id, which the auth middleware attaches
    # to request.state.sdk_token after validating the Bearer token.
    sdk_token_id = request.state.sdk_token.id

    try:
        result = await proxy.call(
            developer=developer,
            provider=provider,
            sdk_token_id=sdk_token_id,
            params=body.params,
            idempotency_key=body.idempotency_key,
        )
        return {
            "success": True,
            "provider": provider.slug,
            "cost_credits": provider.cost_per_call,
            "data": result,
        }

    except InsufficientCreditsError:
        raise HTTPException(
            status_code=402,
            detail="Insufficient credits. Top up your wallet at /v1/wallet/topup."
        )
    except ProviderError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Provider error: {e.message}"
        )