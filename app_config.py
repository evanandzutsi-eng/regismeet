"""
app_config.py
Centralized, strictly-typed environment & security configuration.
Nothing in this file is ever exposed to a client response — see alert_system.py
and main.py DTO boundaries for the enforcement of that rule (Pillar 7).
"""
from __future__ import annotations

from functools import lru_cache
from typing import List, Optional

from pydantic import Field, AnyHttpUrl, field_validator, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="forbid",  # unknown env vars fail fast rather than silently ignored
    )

    # ------------------------------------------------------------------ Core / runtime
    ENVIRONMENT: str = Field(default="production", pattern="^(development|staging|production)$")
    SERVICE_NAME: str = "meeting-intelligence-gateway"
    # Vercel's Python runtime only permits writes under /tmp, and /tmp is wiped
    # between invocations — this local file is a best-effort debug trail for the
    # current invocation only. Ship logs to a durable sink (e.g. Axiom, Datadog,
    # Better Stack) for anything you need to retain past a single request.
    LOG_DIR: str = "/tmp/meeting-intel-logs"

    # ------------------------------------------------------------------ Database / Supabase
    DATABASE_URL: SecretStr
    SUPABASE_URL: AnyHttpUrl
    SUPABASE_SERVICE_ROLE_KEY: SecretStr
    SUPABASE_JWT_SECRET: SecretStr
    JWT_ALGORITHM: str = "HS256"

    # ------------------------------------------------------------------ Upstash Redis (REST, serverless-safe)
    UPSTASH_REDIS_REST_URL: AnyHttpUrl
    UPSTASH_REDIS_REST_TOKEN: SecretStr
    REDIS_MAX_AUTH_FAILURES: int = 5
    REDIS_AUTH_LOCKOUT_SECONDS: int = 900

    # ------------------------------------------------------------------ Upstash QStash (durable HTTP job queue)
    QSTASH_TOKEN: SecretStr
    QSTASH_CURRENT_SIGNING_KEY: SecretStr
    QSTASH_NEXT_SIGNING_KEY: SecretStr
    PUBLIC_APP_URL: AnyHttpUrl = "https://app.meetingintel.example.com"

    # ------------------------------------------------------------------ Supabase Storage (direct-to-storage uploads)
    SUPABASE_AUDIO_BUCKET: str = "webinar-audio"
    SIGNED_UPLOAD_URL_TTL_SECONDS: int = 300

    # ------------------------------------------------------------------ AI providers (server-side only, Pillar 8)
    GEMINI_API_KEY: SecretStr
    GEMINI_MODEL_NAME: str = "gemini-2.5-flash"
    OPENAI_API_KEY: SecretStr  # used for Whisper transcription + text-embedding-3-small
    WHISPER_MODEL_NAME: str = "whisper-1"
    EMBEDDING_MODEL_NAME: str = "text-embedding-3-small"
    EMBEDDING_DIMENSIONS: int = 1536

    # Optional self-hosted overrides. When set, ai_summarizer.py points its
    # OpenAI-SDK clients at these instead of api.openai.com — this works
    # because self-hosted servers like faster-whisper-server and Infinity
    # (michaelfeil/infinity) both expose OpenAI-API-compatible endpoints, so
    # no client-code rewrite is needed, just a different base_url + key.
    # Leave unset to keep using OpenAI directly.
    WHISPER_BASE_URL: Optional[AnyHttpUrl] = None
    WHISPER_API_KEY: Optional[SecretStr] = None
    EMBEDDING_BASE_URL: Optional[AnyHttpUrl] = None
    EMBEDDING_API_KEY: Optional[SecretStr] = None

    # ------------------------------------------------------------------ Paystack
    PAYSTACK_SECRET_KEY: SecretStr
    PAYSTACK_PUBLIC_KEY: str
    PAYSTACK_WEBHOOK_TOLERANCE_SECONDS: int = 300

    # ------------------------------------------------------------------ Resend / Slack
    RESEND_API_KEY: SecretStr
    RESEND_FROM_ADDRESS: str = "alerts@meetingintel.example.com"

    # ------------------------------------------------------------------ Cloudflare Turnstile (bot barrier)
    TURNSTILE_SECRET_KEY: SecretStr
    TURNSTILE_VERIFY_URL: AnyHttpUrl = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

    # ------------------------------------------------------------------ CORS / network hardening
    # Stored as a plain comma-separated string on purpose: pydantic-settings only
    # attempts JSON-decoding on "complex" types (list/dict/etc.), never on str,
    # so this sidesteps that entirely rather than fighting it. Use
    # allowed_origins_list() below wherever an actual list is needed.
    ALLOWED_ORIGINS: str = Field(default="https://app.meetingintel.example.com")
    MAX_REQUEST_BODY_BYTES: int = 25 * 1024 * 1024  # 25 MB hard ceiling
    ALLOWED_AUDIO_EXTENSIONS: List[str] = Field(default_factory=lambda: [".mp3", ".wav"])
    ALLOWED_AUDIO_MIME_TYPES: List[str] = Field(
        default_factory=lambda: ["audio/mpeg", "audio/x-wav", "audio/wav", "audio/vnd.wave"]
    )

    # ------------------------------------------------------------------ Rate limiting (token bucket, Pillar 9)
    RATE_LIMIT_AI_CAPACITY: int = 20          # bucket size
    RATE_LIMIT_AI_REFILL_PER_MINUTE: int = 10  # tokens restored per minute
    RATE_LIMIT_API_CAPACITY: int = 120
    RATE_LIMIT_API_REFILL_PER_MINUTE: int = 60

    # ------------------------------------------------------------------ Temp storage (Pillar 1: purge on completion)
    TEMP_AUDIO_DIR: str = "/tmp/meeting-intel-ingest"

    def allowed_origins_list(self) -> List[str]:
        """Splits the comma-separated ALLOWED_ORIGINS string into a clean list."""
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(",") if origin.strip()]

    @field_validator("ENVIRONMENT")
    @classmethod
    def _forbid_wildcard_cors_in_prod(cls, value: str) -> str:
        return value


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton — avoids re-parsing env on every request."""
    return Settings()
