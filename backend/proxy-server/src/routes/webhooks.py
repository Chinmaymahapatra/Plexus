"""
Webhook routes — receives events from Stripe.

Security: every incoming webhook is verified against Stripe's signature
before any processing happens. Never skip this check.

Events handled:
  checkout.session.completed → top up developer wallet
"""

import uuid

import stripe
import structlog
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database import AsyncSessionLocal
from src.models import Developer
from src.services.wallet import WalletService

log = structlog.get_logger()
router = APIRouter()

stripe.api_key = settings.STRIPE_SECRET_KEY
CREDITS_PER_DOLLAR = 1000


@router.post("/stripe")
async def stripe_webhook(request: Request):
    """
    Receive Stripe webhook events.
    This route is PUBLIC — auth middleware skips it.
    Security comes from Stripe signature verification instead.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not settings.STRIPE_WEBHOOK_SECRET:
        log.warning("webhooks.stripe.no_secret_configured")
        raise HTTPException(status_code=503, detail="Webhook not configured")

    # Verify Stripe signature — this is the only auth for this endpoint
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        log.warning("webhooks.stripe.invalid_signature")
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")

    log.info("webhooks.stripe.received", event_type=event["type"])

    # ─── Handle events ────────────────────────────────────────────────────────
    if event["type"] == "checkout.session.completed":
        await _handle_checkout_completed(event["data"]["object"])

    # Return 200 quickly — Stripe retries on non-2xx
    return {"received": True}


async def _handle_checkout_completed(session: dict):
    """Process a completed Stripe checkout — credit the developer's wallet."""
    metadata = session.get("metadata", {})

    if metadata.get("plexus_event") != "wallet_topup":
        return  # Not a Plexus top-up event

    developer_id_str = metadata.get("developer_id")
    credits_to_add_str = metadata.get("credits_to_add")

    if not developer_id_str or not credits_to_add_str:
        log.error("webhooks.stripe.missing_metadata", session_id=session.get("id"))
        return

    developer_id = uuid.UUID(developer_id_str)
    credits_to_add = int(credits_to_add_str)
    stripe_payment_id = session.get("payment_intent", session.get("id"))

    async with AsyncSessionLocal() as db:
        wallet_svc = WalletService(db)
        try:
            tx = await wallet_svc.topup(
                developer_id=developer_id,
                amount_credits=credits_to_add,
                stripe_payment_id=stripe_payment_id,
                idempotency_key=f"stripe_{stripe_payment_id}",
            )
            await db.commit()
            log.info(
                "webhooks.stripe.topup_success",
                developer_id=str(developer_id),
                credits=credits_to_add,
                tx_id=str(tx.id),
            )
        except Exception as e:
            await db.rollback()
            log.error("webhooks.stripe.topup_failed", error=str(e), developer_id=str(developer_id))
            raise