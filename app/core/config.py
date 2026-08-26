"""
Centralized application configuration.
All secrets are pulled from environment variables (.env in local dev).
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Supabase
    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str  # backend uses the service role key (bypasses RLS safely, server-side only)
    # No longer required for token verification (see db/supabase_client.py) — kept optional
    # in case you switch to local JWKS-based verification later.
    SUPABASE_JWT_SECRET: str | None = None

    # LLM providers — Groq preferred for speed/cost, OpenAI as fallback
    GROQ_API_KEY: str | None = None
    OPENAI_API_KEY: str | None = None
    LLM_PROVIDER: str = "groq"          # "groq" | "openai"
    GROQ_MODEL: str = "qwen/qwen3-32b" #"llama-3.3-70b-versatile"
    OPENAI_MODEL: str = "gpt-4o-mini"

    # Email (SMTP) — used for critical-alert notifications and reminder emails.
    # All optional: notification/reminder sends are skipped silently if unset.
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM_EMAIL: str | None = None

    # Scheduler — enables the nightly recurring-report-reminder job
    ENABLE_SCHEDULER: bool = False

    # Public frontend URL, used to build shareable report links in emails
    FRONTEND_URL: str = "http://localhost:5173"

    # White-label custom domains: agencies point their domain's CNAME record
    # at this hostname. Set this to wherever your frontend is actually
    # deployed (e.g. "app.rakh.io" or your load balancer's hostname).
    CUSTOM_DOMAIN_CNAME_TARGET: str = "app.rakh.io"

    # Token encryption — Fernet key used to encrypt OAuth tokens at rest.
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Required before any platform connection can be created.
    TOKEN_ENCRYPTION_KEY: str | None = None

    # Google Ads API (Phase 1 — the only connector live so far; no app
    # review required for a standard developer token, unlike Meta/TikTok)
    GOOGLE_OAUTH_CLIENT_ID: str | None = None
    GOOGLE_OAUTH_CLIENT_SECRET: str | None = None
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:8000/api/integrations/google/callback"
    GOOGLE_ADS_DEVELOPER_TOKEN: str | None = None

    # Meta Ads (Phase 2 — connector code complete, gated by Meta App
    # Review + Business Verification before it works on real client
    # accounts; see meta_ads_connector.py for details).
    META_OAUTH_APP_ID: str | None = None
    META_OAUTH_APP_SECRET: str | None = None
    META_OAUTH_REDIRECT_URI: str = "http://localhost:8000/api/integrations/meta/callback"

    # TikTok Ads (Phase 3 — connector code complete, gated by TikTok
    # Marketing API review before it works on real client accounts; see
    # tiktok_ads_connector.py for details).
    TIKTOK_OAUTH_APP_ID: str | None = None
    TIKTOK_OAUTH_APP_SECRET: str | None = None
    TIKTOK_OAUTH_REDIRECT_URI: str = "http://localhost:8000/api/integrations/tiktok/callback"

    # App
    ENVIRONMENT: str = "development"
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]
    MAX_UPLOAD_SIZE_MB: int = 15

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
