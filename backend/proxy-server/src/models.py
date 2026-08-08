"""
Plexus database models.

Design principles:
  - Wallets never store a raw balance. Balance = SUM(transactions). This is
    how banks work — immutable ledger entries, never mutated totals.
  - All monetary values are stored as INTEGER credits (not floats) to avoid
    floating-point precision bugs. 1 credit = $0.0001 USD at our default rate.
  - Every table has created_at / updated_at for audit purposes.
"""
import sqlalchemy as sa
import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import (
    String,
    Integer,
    BigInteger,
    Boolean,
    DateTime,
    Text,
    ForeignKey,
    UniqueConstraint,
    Index,
    Numeric,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB
from pgvector.sqlalchemy import Vector

from src.database import Base


def utcnow():
    return datetime.now(timezone.utc)


# ─── Enums ────────────────────────────────────────────────────────────────────


class TransactionStatus(str, PyEnum):
    PENDING = "pending"  # debit locked, API call in flight
    SETTLED = "settled"  # API call succeeded, debit confirmed
    REFUNDED = "refunded"  # API call failed, credit restored
    FAILED = "failed"  # debit failed (insufficient credits)


class TransactionType(str, PyEnum):
    TOPUP = "topup"  # developer added credits
    DEBIT = "debit"  # agent made an API call
    REFUND = "refund"  # API call failed, credits returned
    PAYOUT = "payout"  # provider received payment


# ─── Users / Developers ───────────────────────────────────────────────────────


class Developer(Base):
    """
    A developer who uses Plexus to give their agents API access.
    One developer can have many SDK tokens (for different projects/agents).
    """

    __tablename__ = "developers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    # Relationships
    sdk_tokens: Mapped[list["SDKToken"]] = relationship(
        back_populates="developer", cascade="all, delete-orphan"
    )
    wallet: Mapped["Wallet"] = relationship(back_populates="developer", uselist=False)
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="developer")


class SDKToken(Base):
    """
    API key a developer embeds in their agent's environment.
    This is NOT the provider's key — it's Plexus's own auth token.
    Format: plx_live_<random> or plx_test_<random>
    """

    __tablename__ = "sdk_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    developer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("developers.id"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )  # SHA-256 of actual token
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="Default")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    developer: Mapped["Developer"] = relationship(back_populates="sdk_tokens")


# ─── Wallet / Ledger ──────────────────────────────────────────────────────────


class Wallet(Base):
    """
    One wallet per developer. The balance here is a CACHE only —
    the source of truth is always SUM(transactions.amount).
    We keep a cached balance for fast pre-call credit checks.
    """

    __tablename__ = "wallets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    developer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("developers.id"), unique=True, nullable=False
    )
    balance_credits: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # locked_credits: credits reserved for in-flight API calls
    locked_credits: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    developer: Mapped["Developer"] = relationship(back_populates="wallet")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="wallet")


class Transaction(Base):
    """
    Immutable ledger entry for every credit movement.
    NEVER delete or update these rows — append only.
    """

    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False
    )  # UUID from caller
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallets.id"), nullable=False
    )
    developer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("developers.id"), nullable=False
    )
    amount_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )  # negative = debit
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    api_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_calls.id"), nullable=True
    )
    stripe_payment_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    call_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    wallet: Mapped["Wallet"] = relationship(back_populates="transactions")
    developer: Mapped["Developer"] = relationship(back_populates="transactions")
    api_call: Mapped["APICall | None"] = relationship(back_populates="transaction")

    __table_args__ = (
        Index("ix_transactions_wallet_id", "wallet_id"),
        Index("ix_transactions_developer_id", "developer_id"),
        Index("ix_transactions_created_at", "created_at"),
    )


# ─── API Registry ─────────────────────────────────────────────────────────────


class APIProvider(Base):
    """
    A listed API provider in the Plexus marketplace.
    embedding: 1536-dim vector of the description for semantic search.
    """

    __tablename__ = "api_providers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False
    )  # e.g. "serpapi"
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(
        String(100), nullable=False
    )  # search | scraping | dataset | ai-model
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    cost_per_call: Mapped[int] = mapped_column(Integer, nullable=False)  # in credits
    openapi_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    auth_type: Mapped[str] = mapped_column(
        String(50), default="api_key"
    )  # api_key | bearer | basic
    auth_header: Mapped[str] = mapped_column(String(100), default="X-API-Key")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.true())    # pgvector column — requires pgvector extension
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    api_calls: Mapped[list["APICall"]] = relationship(back_populates="provider")

    __table_args__ = (
        Index("ix_api_providers_category", "category"),
        # Vector index created separately in migration (requires HNSW or IVFFlat)
    )


# ─── API Call Log ─────────────────────────────────────────────────────────────


class APICall(Base):
    """
    Every single proxied API call is logged here.
    This is your audit trail and the basis for provider payouts.
    """

    __tablename__ = "api_calls"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    developer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("developers.id"), nullable=False
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("api_providers.id"), nullable=False
    )
    sdk_token_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sdk_tokens.id"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(String(500), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_credits: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # We deliberately do NOT log request/response bodies — privacy + cost
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    provider: Mapped["APIProvider"] = relationship(back_populates="api_calls")
    transaction: Mapped["Transaction | None"] = relationship(back_populates="api_call")

    __table_args__ = (
        Index("ix_api_calls_developer_id", "developer_id"),
        Index("ix_api_calls_provider_id", "provider_id"),
        Index("ix_api_calls_created_at", "created_at"),
    )
