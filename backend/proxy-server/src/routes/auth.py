"""
Auth routes — developer registration, login, SDK token management.

Flow:
  1. Developer registers → wallet created → free credits granted
  2. Developer logs in → receives a short-lived JWT (for dashboard API)
  3. Developer creates SDK token → embeds it in their agent
  4. Agent uses SDK token as Bearer token for /v1/call requests
"""

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from jose import jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database import get_db
from src.models import Developer, SDKToken, Wallet
from src.services.wallet import WalletService

router = APIRouter()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ─── Schemas ──────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    name: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class CreateTokenRequest(BaseModel):
    name: str = "Default"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def create_jwt(developer_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRY_MINUTES)
    return jwt.encode(
        {"sub": developer_id, "exp": expire},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def generate_sdk_token(is_live: bool = False) -> tuple[str, str]:
    """
    Returns (raw_token, token_hash).
    Raw token is shown to developer once, never stored.
    Hash is stored in DB for validation.
    """
    prefix = "plx_live" if is_live else "plx_test"
    raw = f"{prefix}_{os.urandom(16).hex()}"
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    return raw, hashed


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.post("/register", status_code=201)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """Register a new developer. Creates wallet and grants free credits."""
    # Check for existing account
    existing = await db.execute(select(Developer).where(Developer.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    # Create developer
    developer = Developer(
        email=body.email,
        hashed_password=pwd_context.hash(body.password),
        name=body.name,
    )
    db.add(developer)
    await db.flush()

    # Create wallet + grant free credits
    wallet_svc = WalletService(db)
    await wallet_svc.grant_free_credits(developer.id, settings.FREE_CREDITS_ON_SIGNUP)

    # Create a default SDK token
    raw_token, token_hash = generate_sdk_token(is_live=False)
    sdk_token = SDKToken(
        developer_id=developer.id,
        token_hash=token_hash,
        name="Default (test)",
    )
    db.add(sdk_token)
    await db.commit()

    return {
        "message": "Account created",
        "developer_id": str(developer.id),
        "free_credits": settings.FREE_CREDITS_ON_SIGNUP,
        # Show the SDK token ONCE — we never store the raw value
        "sdk_token": raw_token,
        "sdk_token_note": "Save this now. It will not be shown again.",
    }


@router.post("/login")
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Login and receive a JWT for dashboard API access."""
    result = await db.execute(select(Developer).where(Developer.email == body.email))
    developer = result.scalar_one_or_none()

    if not developer or not pwd_context.verify(body.password, developer.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not developer.is_active:
        raise HTTPException(status_code=403, detail="Account suspended")

    return {
        "access_token": create_jwt(str(developer.id)),
        "token_type": "bearer",
        "developer_id": str(developer.id),
    }


@router.post("/tokens", status_code=201)
async def create_sdk_token(
    body: CreateTokenRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create a new SDK token for a developer's project or agent."""
    developer: Developer = request.state.developer

    raw_token, token_hash = generate_sdk_token(is_live=settings.ENVIRONMENT == "production")
    sdk_token = SDKToken(
        developer_id=developer.id,
        token_hash=token_hash,
        name=body.name,
    )
    db.add(sdk_token)
    await db.commit()

    return {
        "token_id": str(sdk_token.id),
        "name": body.name,
        "sdk_token": raw_token,
        "note": "Save this now. It will not be shown again.",
    }


@router.get("/tokens")
async def list_sdk_tokens(request: Request, db: AsyncSession = Depends(get_db)):
    """List all SDK tokens for the authenticated developer."""
    developer: Developer = request.state.developer
    result = await db.execute(
        select(SDKToken)
        .where(SDKToken.developer_id == developer.id)
        .where(SDKToken.is_active == True)
    )
    tokens = result.scalars().all()

    return {
        "tokens": [
            {
                "id": str(t.id),
                "name": t.name,
                "last_used_at": t.last_used_at,
                "created_at": t.created_at,
            }
            for t in tokens
        ]
    }