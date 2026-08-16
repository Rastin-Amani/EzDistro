"""Application configuration (pydantic-settings, env-driven)."""

import hashlib
import uuid
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- app ---
    env: str = "dev"  # dev | production
    app_version: str = "0.1.0"
    app_name: str = "Seoz Platform"

    # --- PocketBase ---
    pb_url: str = "https://db.seoz.rastin.cloud"
    pb_admin_email: str = ""
    pb_admin_password: str = ""

    # Fernet key (32 url-safe base64 bytes) used to encrypt stored credentials.
    # In dev an empty value derives a stable key; production MUST set SECRETS_KEY.
    secrets_key: str = ""

    # --- Qdrant (default; per-project config lives in credentials) ---
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str = ""

    # SSRF guard: reject private/loopback/link-local target hosts unless enabled (local dev).
    allow_private_networks: bool = False

    # --- worker process ---
    worker_id: str = ""  # empty → auto-generated per process
    poll_interval_seconds: float = 5.0
    lease_seconds: int = 300  # job lease duration
    heartbeat_interval_seconds: float = 15.0
    max_concurrent_jobs: int = 4  # global worker concurrency cap
    schedule_poll_interval_seconds: float = 60.0
    schedule_window_minutes: int = 15  # how far ahead due schedules are picked up

    # expose Ollama as a selectable LLM provider (intentionally opt-in)
    ollama_enabled: bool = False

    # --- bounded provider concurrency (rate limits) ---
    llm_concurrency: int = 4  # max concurrent LLM calls per worker
    embedding_concurrency: int = 4  # max concurrent embedding batches per worker
    publish_concurrency: int = 2  # max concurrent WordPress publishes per worker

    # --- observability ---
    # record every provider call as a job_event (opt-in; default is debug logs)
    provider_events_enabled: bool = False

    # --- web ---
    page_size: int = 25  # default pagination for UI lists

    @property
    def is_prod(self) -> bool:
        return self.env.lower() == "production"

    @property
    def derived_worker_id(self) -> str:
        return self.worker_id or uuid.uuid4().hex[:12]

    @property
    def effective_secrets_key(self) -> bytes:
        """Return the Fernet key bytes.

        Production requires SECRETS_KEY (a predictable dev-derived key would
        make stored provider credentials recoverable by anyone). The dev
        fallback is deterministic per environment and only allowed outside prod.
        """
        if self.secrets_key:
            return self.secrets_key.encode("utf-8")
        if self.is_prod:
            raise RuntimeError(
                "SECRETS_KEY must be set in production (32 bytes, url-safe base64) — "
                "provider credentials are encrypted with it at rest."
            )
        digest = hashlib.sha256(f"seoz-dev-key::{self.pb_url}::{self.env}".encode()).digest()
        import base64

        return base64.urlsafe_b64encode(digest)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
