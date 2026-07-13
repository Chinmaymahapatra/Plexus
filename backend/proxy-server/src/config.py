"""
Central config — all environment variables loaded here.
Access anywhere with: from src.config import settings
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    DATABASE_URL: str
    REDIS_URL: str = "redis://localhost:6379/0"

    # Auth
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_MINUTES: int = 10080  # 7 days

    # Stripe
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""

    # AWS
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "ap-south-1"

    # Embeddings
    OPENAI_API_KEY: str = ""

    # App
    ENVIRONMENT: str = "development"
    FREE_CREDITS_ON_SIGNUP: int = 10000
    PLEXUS_MARGIN_PERCENT: float = 30.0

    # Local dev fallback keys (never in production)
    DEV_SERPAPI_KEY: str = ""
    DEV_APIFY_KEY: str = ""
    DEV_OPENWEATHER_KEY: str = ""

    @property
    def is_dev(self) -> bool:
        return self.ENVIRONMENT == "development"

    @property
    def use_aws_secrets(self) -> bool:
        """In prod, always use AWS Secrets Manager. In dev, fall back to env vars."""
        return self.ENVIRONMENT == "production"


settings = Settings()