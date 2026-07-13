"""
API Registry routes — browse and search the Plexus marketplace.

GET /v1/registry              → list all APIs (paginated, filterable by category)
GET /v1/registry/search       → semantic search by natural language intent
GET /v1/registry/{slug}       → get a single API's details
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models import APIProvider

router = APIRouter()

VALID_CATEGORIES = {"search", "scraping", "dataset", "ai-model", "finance", "communication"}


@router.get("")
async def list_apis(
    db: AsyncSession = Depends(get_db),
    category: Optional[str] = Query(None, description="Filter by category"),
    limit: int = Query(20, le=100),
    offset: int = Query(0),
):
    query = select(APIProvider).where(APIProvider.is_active == True)
    if category:
        query = query.where(APIProvider.category == category)
    query = query.limit(limit).offset(offset)

    result = await db.execute(query)
    providers = result.scalars().all()

    return {
        "apis": [_format_provider(p) for p in providers],
        "limit": limit,
        "offset": offset,
    }


@router.get("/search")
async def semantic_search(
    q: str = Query(..., min_length=3, description="Natural language intent"),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(5, le=20),
):
    """
    Semantic search using pgvector cosine similarity.
    Phase 1: returns name-match results (full semantic search in Phase 2).

    Phase 2 implementation:
      1. Embed the query with OpenAI text-embedding-3-small
      2. Run: SELECT * FROM api_providers ORDER BY embedding <=> $1 LIMIT $2
    """
    # Phase 1: simple text matching (works without embeddings)
    result = await db.execute(
        select(APIProvider)
        .where(APIProvider.is_active == True)
        .where(
            APIProvider.name.ilike(f"%{q}%") |
            APIProvider.description.ilike(f"%{q}%") |
            APIProvider.category.ilike(f"%{q}%")
        )
        .limit(limit)
    )
    providers = result.scalars().all()

    return {
        "query": q,
        "results": [_format_provider(p) for p in providers],
        "search_type": "text_match",  # will be "semantic" in Phase 2
    }


@router.get("/{slug}")
async def get_api(slug: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(APIProvider)
        .where(APIProvider.slug == slug)
        .where(APIProvider.is_active == True)
    )
    provider = result.scalar_one_or_none()

    if not provider:
        raise HTTPException(status_code=404, detail=f"API '{slug}' not found")

    return _format_provider(provider, include_schema=True)


def _format_provider(p: APIProvider, include_schema: bool = False) -> dict:
    data = {
        "slug": p.slug,
        "name": p.name,
        "description": p.description,
        "category": p.category,
        "cost_per_call_credits": p.cost_per_call,
        "cost_per_call_usd": round(p.cost_per_call / 1000, 6),
        "auth_type": p.auth_type,
    }
    if include_schema and p.openapi_schema:
        data["openapi_schema"] = p.openapi_schema
    return data