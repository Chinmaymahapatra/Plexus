"""
Wallet routes.

GET  /v1/wallet/balance    → current balance and stats
POST /v1/wallet/topup      → create a Stripe Checkout session
GET  /v1/wallet/history    → paginated transaction history
"""

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database import get_db
from src.models import Transaction, TransactionType
from src.services.wallet import WalletService

stripe.api_key = settings.STRIPE_SECRET_KEY

router = APIRouter()

# Credit pricing: 1000 credits = $1 USD
CREDITS_PER_DOLLAR = 1000


class TopupRequest(BaseModel):
    amount_usd: float = Field(..., ge=5.0, le=1000.0, description="Amount in USD ($5 minimum)")
    success_url: str = "https://app.plexus.dev/wallet?topup=success"
    cancel_url:  str = "https://app.plexus.dev/wallet?topup=cancelled"


@router.get("/balance")
async def get_balance(request: Request, db: AsyncSession = Depends(get_db)):
    developer = request.state.developer
    wallet_svc = WalletService(db)

    balance = await wallet_svc.get_balance(developer.id)
    wallet = await wallet_svc.get_or_create_wallet(developer.id)

    return {
        "balance_credits": balance,
        "locked_credits": wallet.locked_credits,
        "balance_usd": round(balance / CREDITS_PER_DOLLAR, 4),
        "credits_per_dollar": CREDITS_PER_DOLLAR,
    }


@router.post("/topup")
async def initiate_topup(
    body: TopupRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Create a Stripe Checkout session for a credit top-up.
    The developer is redirected to Stripe to complete payment.
    Credits are added by the Stripe webhook (POST /v1/webhooks/stripe).
    """
    developer = request.state.developer

    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Stripe not configured")

    credits_to_add = int(body.amount_usd * CREDITS_PER_DOLLAR)

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": f"Plexus Credits — {credits_to_add:,} credits",
                        "description": f"${body.amount_usd:.2f} = {credits_to_add:,} credits",
                    },
                    "unit_amount": int(body.amount_usd * 100),  # Stripe uses cents
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=body.success_url,
            cancel_url=body.cancel_url,
            metadata={
                "developer_id": str(developer.id),
                "credits_to_add": str(credits_to_add),
                "plexus_event": "wallet_topup",
            },
        )
        return {
            "checkout_url": session.url,
            "session_id": session.id,
            "credits_to_add": credits_to_add,
        }

    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/history")
async def transaction_history(
    request: Request,
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
):
    developer = request.state.developer

    result = await db.execute(
        select(Transaction)
        .where(Transaction.developer_id == developer.id)
        .order_by(desc(Transaction.created_at))
        .limit(limit)
        .offset(offset)
    )
    txs = result.scalars().all()

    return {
        "transactions": [
            {
                "id": str(tx.id),
                "type": tx.type,
                "status": tx.status,
                "amount_credits": tx.amount_credits,
                "amount_usd": round(abs(tx.amount_credits) / CREDITS_PER_DOLLAR, 4),
                "created_at": tx.created_at,
            }
            for tx in txs
        ],
        "limit": limit,
        "offset": offset,
    }