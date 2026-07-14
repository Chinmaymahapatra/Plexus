"""Initial schema — all Plexus tables.

Revision ID: 001
Create Date: 2026-06-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB
import uuid

revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        'developers',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column('email', sa.String(255), unique=True, nullable=False),
        sa.Column('hashed_password', sa.String(255), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('is_active', sa.Boolean, default=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        'sdk_tokens',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column('developer_id', UUID(as_uuid=True), sa.ForeignKey('developers.id'), nullable=False),
        sa.Column('token_hash', sa.String(64), unique=True, nullable=False),
        sa.Column('name', sa.String(100), nullable=False, default='Default'),
        sa.Column('is_active', sa.Boolean, default=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_sdk_tokens_developer_id', 'sdk_tokens', ['developer_id'])

    op.create_table(
        'wallets',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column('developer_id', UUID(as_uuid=True), sa.ForeignKey('developers.id'), unique=True, nullable=False),
        sa.Column('balance_credits', sa.BigInteger, default=0, nullable=False),
        sa.Column('locked_credits', sa.BigInteger, default=0, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        'api_providers',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column('slug', sa.String(100), unique=True, nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.Text, nullable=False),
        sa.Column('category', sa.String(100), nullable=False),
        sa.Column('base_url', sa.String(500), nullable=False),
        sa.Column('cost_per_call', sa.Integer, nullable=False),
        sa.Column('openapi_schema', JSONB, nullable=True),
        sa.Column('auth_type', sa.String(50), default='api_key'),
        sa.Column('auth_header', sa.String(100), default='X-API-Key'),
        sa.Column('is_active', sa.Boolean, default=True),
        # pgvector column — 1536 dims for text-embedding-3-small
        sa.Column('embedding', sa.Text, nullable=True),  # stored as vector type via raw SQL below
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_api_providers_category', 'api_providers', ['category'])

    # Alter embedding column to proper vector type after table creation
    op.execute("ALTER TABLE api_providers ALTER COLUMN embedding TYPE vector(1536) USING NULL::vector(1536)")

    # HNSW index for fast ANN search (better than IVFFlat for small datasets)
    op.execute("""
        CREATE INDEX ix_api_providers_embedding_hnsw
        ON api_providers
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)

    op.create_table(
        'api_calls',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column('developer_id', UUID(as_uuid=True), sa.ForeignKey('developers.id'), nullable=False),
        sa.Column('provider_id', UUID(as_uuid=True), sa.ForeignKey('api_providers.id'), nullable=False),
        sa.Column('sdk_token_id', UUID(as_uuid=True), sa.ForeignKey('sdk_tokens.id'), nullable=False),
        sa.Column('endpoint', sa.String(500), nullable=False),
        sa.Column('method', sa.String(10), nullable=False),
        sa.Column('status_code', sa.Integer, nullable=True),
        sa.Column('cost_credits', sa.Integer, nullable=False),
        sa.Column('latency_ms', sa.Integer, nullable=True),
        sa.Column('error', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_api_calls_developer_id', 'api_calls', ['developer_id'])
    op.create_index('ix_api_calls_provider_id', 'api_calls', ['provider_id'])
    op.create_index('ix_api_calls_created_at', 'api_calls', ['created_at'])

    op.create_table(
        'transactions',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column('idempotency_key', sa.String(255), unique=True, nullable=False),
        sa.Column('wallet_id', UUID(as_uuid=True), sa.ForeignKey('wallets.id'), nullable=False),
        sa.Column('developer_id', UUID(as_uuid=True), sa.ForeignKey('developers.id'), nullable=False),
        sa.Column('amount_credits', sa.BigInteger, nullable=False),
        sa.Column('type', sa.String(20), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('api_call_id', UUID(as_uuid=True), sa.ForeignKey('api_calls.id'), nullable=True),
        sa.Column('stripe_payment_id', sa.String(255), nullable=True),
        sa.Column('call_metadata', JSONB, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_transactions_wallet_id', 'transactions', ['wallet_id'])
    op.create_index('ix_transactions_developer_id', 'transactions', ['developer_id'])
    op.create_index('ix_transactions_created_at', 'transactions', ['created_at'])

    # Seed initial API providers (5 for MVP)
    op.execute("""
        INSERT INTO api_providers (id, slug, name, description, category, base_url, cost_per_call, auth_type, auth_header)
        VALUES
          (gen_random_uuid(), 'serpapi', 'SerpAPI', 'Google Search results in structured JSON. Ideal for research agents that need web search.', 'search', 'https://serpapi.com/search', 2, 'api_key', 'api_key'),
          (gen_random_uuid(), 'openweather', 'OpenWeatherMap', 'Real-time and forecast weather data for any city worldwide.', 'dataset', 'https://api.openweathermap.org/data/2.5/weather', 1, 'api_key', 'appid'),
          (gen_random_uuid(), 'newsapi', 'NewsAPI', 'Latest news articles from 80,000+ sources. Search by keyword, source, or topic.', 'search', 'https://newsapi.org/v2/everything', 1, 'api_key', 'apiKey'),
          (gen_random_uuid(), 'coingecko', 'CoinGecko', 'Cryptocurrency prices, market data, and historical charts. Free tier available.', 'finance', 'https://api.coingecko.com/api/v3/simple/price', 1, 'api_key', 'x-cg-demo-api-key'),
          (gen_random_uuid(), 'apify-scraper', 'Apify Web Scraper', 'Scrape any public website and extract structured data. Handles JS-rendered pages.', 'scraping', 'https://api.apify.com/v2/acts/apify~web-scraper/runs', 5, 'bearer', 'Authorization')
    """)


def downgrade() -> None:
    op.drop_table('transactions')
    op.drop_table('api_calls')
    op.drop_table('api_providers')
    op.drop_table('wallets')
    op.drop_table('sdk_tokens')
    op.drop_table('developers')
    op.execute("DROP EXTENSION IF EXISTS vector")