"""
Wallet Service — all credit operations go through here.

The two hardest problems in this file:
  1. Atomic debit: lock credits BEFORE the API call, refund on failure.
  2. Idempotency: same idempotency_key never charges twice.

Every credit movement is an immutable Transaction row.
The wallet.balance_credits is a cached total for fast reads,
but the ledger (transactions table) is always the source of truth.
"""

import uuid
from typing import Optional

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Wallet, Transaction, TransactionType, TransactionStatus, Developer

log = structlog.get_logger()


class InsufficientCreditsError(Exception):
    pass


class IdempotencyConflictError(Exception):
    pass


class WalletService:

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_or_create_wallet(self, developer_id: uuid.UUID) -> Wallet:
        """Get a developer's wallet, creating it if it doesn't exist."""
        result = await self.db.execute(
            select(Wallet).where(Wallet.developer_id == developer_id)
        )
        wallet = result.scalar_one_or_none()

        if not wallet:
            wallet = Wallet(developer_id=developer_id, balance_credits=0)
            self.db.add(wallet)
            await self.db.flush()
            log.info("wallet.created", developer_id=str(developer_id))

        return wallet

    async def get_balance(self, developer_id: uuid.UUID) -> int:
        """Return available credits (balance minus locked)."""
        wallet = await self.get_or_create_wallet(developer_id)
        return wallet.balance_credits - wallet.locked_credits

    async def topup(
        self,
        developer_id: uuid.UUID,
        amount_credits: int,
        stripe_payment_id: str,
        idempotency_key: str,
    ) -> Transaction:
        """
        Credit a developer's wallet after a successful Stripe payment.
        Called from the Stripe webhook handler.
        Idempotent: same stripe_payment_id is a no-op (returns existing tx).
        """
        # Check for duplicate (Stripe can fire webhooks multiple times)
        existing = await self.db.execute(
            select(Transaction).where(
                Transaction.idempotency_key == idempotency_key
            )
        )
        if existing.scalar_one_or_none():
            log.info("wallet.topup.duplicate", idempotency_key=idempotency_key)
            return existing.scalar_one()

        wallet = await self.get_or_create_wallet(developer_id)

        # Atomically update balance and create transaction
        wallet.balance_credits += amount_credits
        tx = Transaction(
            idempotency_key=idempotency_key,
            wallet_id=wallet.id,
            developer_id=developer_id,
            amount_credits=amount_credits,
            type=TransactionType.TOPUP.value,
            status=TransactionStatus.SETTLED.value,
            stripe_payment_id=stripe_payment_id,
        )
        self.db.add(tx)
        await self.db.flush()

        log.info(
            "wallet.topup.success",
            developer_id=str(developer_id),
            credits=amount_credits,
            new_balance=wallet.balance_credits,
        )
        return tx

    async def debit_lock(
        self,
        developer_id: uuid.UUID,
        amount_credits: int,
        idempotency_key: str,
        api_call_id: Optional[uuid.UUID] = None,
    ) -> Transaction:
        """
        Phase 1 of two-phase commit: lock credits before the API call.
        Creates a PENDING transaction and increments locked_credits.
        Call debit_settle() or debit_refund() after the API call completes.
        """
        # Idempotency check
        existing = await self.db.execute(
            select(Transaction).where(Transaction.idempotency_key == idempotency_key)
        )
        if existing.scalar_one_or_none():
            raise IdempotencyConflictError(f"Transaction {idempotency_key} already exists")

        wallet = await self.get_or_create_wallet(developer_id)
        available = wallet.balance_credits - wallet.locked_credits

        if available < amount_credits:
            raise InsufficientCreditsError(
                f"Need {amount_credits} credits, only {available} available"
            )

        # Lock the credits
        wallet.locked_credits += amount_credits

        tx = Transaction(
            idempotency_key=idempotency_key,
            wallet_id=wallet.id,
            developer_id=developer_id,
            amount_credits=-amount_credits,  # negative = debit
            type=TransactionType.DEBIT.value,
            status=TransactionStatus.PENDING.value,
            api_call_id=api_call_id,
        )
        self.db.add(tx)
        await self.db.flush()

        log.info(
            "wallet.debit.locked",
            developer_id=str(developer_id),
            credits=amount_credits,
            tx_id=str(tx.id),
        )
        return tx

    async def debit_settle(self, transaction_id: uuid.UUID) -> Transaction:
        """
        Phase 2a: API call succeeded. Deduct from balance, release lock.
        """
        tx = await self.db.get(Transaction, transaction_id)
        if not tx or tx.status != TransactionStatus.PENDING.value:
            raise ValueError(f"Transaction {transaction_id} not in PENDING state")

        wallet = await self.db.get(Wallet, tx.wallet_id)
        amount = abs(tx.amount_credits)

        # Deduct from balance and release lock
        wallet.balance_credits -= amount
        wallet.locked_credits  -= amount

        tx.status = TransactionStatus.SETTLED.value
        await self.db.flush()

        log.info("wallet.debit.settled", tx_id=str(transaction_id), credits=amount)
        return tx

    async def debit_refund(self, transaction_id: uuid.UUID) -> Transaction:
        """
        Phase 2b: API call failed. Release the lock without touching balance.
        """
        tx = await self.db.get(Transaction, transaction_id)
        if not tx or tx.status != TransactionStatus.PENDING.value:
            raise ValueError(f"Transaction {transaction_id} not in PENDING state")

        wallet = await self.db.get(Wallet, tx.wallet_id)
        amount = abs(tx.amount_credits)

        # Just release the lock — balance unchanged
        wallet.locked_credits -= amount
        tx.status = TransactionStatus.REFUNDED.value
        await self.db.flush()

        log.info("wallet.debit.refunded", tx_id=str(transaction_id), credits=amount)
        return tx

    async def grant_free_credits(
        self, developer_id: uuid.UUID, amount_credits: int
    ) -> Transaction:
        """Grant free signup credits to a new developer."""
        return await self.topup(
            developer_id=developer_id,
            amount_credits=amount_credits,
            stripe_payment_id=f"free_signup_{developer_id}",
            idempotency_key=f"free_signup_{developer_id}",
        )