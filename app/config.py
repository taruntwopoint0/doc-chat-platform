"""Application settings. Every value comes from the environment.

No model names, secrets, or tunables are hardcoded in pipeline code -- anything
adjustable lives here so deployments differ only by their env file.
"""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Infrastructure -------------------------------------------------
    database_url: str = Field(alias="DATABASE_URL")
    #: Shared token, kept for scripts, tests and CI. People sign in instead.
    admin_token: str = Field(alias="ADMIN_TOKEN")

    # --- Sign-in ---------------------------------------------------------
    #: Off by default: the dashboard opens straight onto the workspace with
    #: no login screen, which is what you want on a local PoC. Turn it on
    #: for any deployment reachable by someone else -- an open instance lets
    #: anyone upload to your corpus and spend your Gemini quota.
    auth_enabled: bool = Field(default=False, alias="AUTH_ENABLED")
    #: The first account, created once on an empty users table so a fresh
    #: deployment can be signed into at all. Ignored afterwards.
    admin_username: str = Field(default="admin", alias="ADMIN_USERNAME")
    admin_password: str = Field(default="", alias="ADMIN_PASSWORD")
    session_ttl_hours: int = Field(default=720, alias="SESSION_TTL_HOURS")
    #: Set true wherever the app is served over HTTPS (Render always is);
    #: false for plain-http local development, or the cookie is dropped.
    cookie_secure: bool = Field(default=False, alias="COOKIE_SECURE")

    # --- Providers ------------------------------------------------------
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    embedding_model: str = Field(alias="EMBEDDING_MODEL")
    llm_model: str = Field(alias="LLM_MODEL")

    # Which concrete class backs each interface. Swapping a provider is an
    # env change, never a code change. See app/registry.py.
    parser_impl: str = Field(default="lite", alias="PARSER")
    embedder_impl: str = Field(default="gemini", alias="EMBEDDER_IMPL")
    llm_impl: str = Field(default="gemini", alias="LLM_IMPL")
    vector_store_impl: str = Field(default="pgvector", alias="VECTOR_STORE_IMPL")
    file_store_impl: str = Field(default="postgres", alias="FILE_STORE_IMPL")

    # --- Ingestion ------------------------------------------------------
    #: The whole upload is held in memory to hash it and hand it to the file
    #: store, so this is really a statement about the instance's RAM. 50 MB
    #: suits a normal machine; render.yaml pins the free tier (512 MB) to 10.
    max_upload_mb: int = Field(default=50, alias="MAX_UPLOAD_MB")
    chunk_target_tokens: int = Field(default=500, alias="CHUNK_TARGET_TOKENS")
    chunk_overlap_ratio: float = Field(default=0.15, alias="CHUNK_OVERLAP_RATIO")
    embedding_dimensions: int = Field(default=768, alias="EMBEDDING_DIMENSIONS")
    embed_batch_size: int = Field(default=32, alias="EMBED_BATCH_SIZE")
    embed_max_retries: int = Field(default=5, alias="EMBED_MAX_RETRIES")
    profile_section_chars: int = Field(default=2000, alias="PROFILE_SECTION_CHARS")
    #: Hard ceiling on a single chunk, from the embedding model's input limit.
    #: The chunker will break even "never split" content rather than exceed it.
    embed_max_input_tokens: int = Field(default=8192, alias="EMBED_MAX_INPUT_TOKENS")
    #: "section" = one LLM call describes every section of a document (cheap,
    #: the default). "chunk" = one call per chunk: sharper headers, but one
    #: request per chunk across a corpus with no size limit.
    context_descriptions: str = Field(default="section", alias="CONTEXT_DESCRIPTIONS")

    # --- Retrieval (Milestone 2) ----------------------------------------
    #: Passages sent to the LLM per question. More context costs tokens and
    #: dilutes attention; fewer risks missing the answer.
    retrieval_top_k: int = Field(default=8, alias="RETRIEVAL_TOP_K")

    # --- Worker ---------------------------------------------------------
    worker_enabled: bool = Field(default=True, alias="WORKER_ENABLED")
    worker_concurrency: int = Field(default=1, alias="WORKER_CONCURRENCY")
    worker_poll_seconds: float = Field(default=2.0, alias="WORKER_POLL_SECONDS")
    worker_job_timeout_seconds: int = Field(
        default=3600, alias="WORKER_JOB_TIMEOUT_SECONDS"
    )
    job_max_attempts: int = Field(default=3, alias="JOB_MAX_ATTEMPTS")

    # --- Parsing --------------------------------------------------------
    soffice_binary: str = Field(default="soffice", alias="SOFFICE_BINARY")
    soffice_timeout_seconds: int = Field(default=180, alias="SOFFICE_TIMEOUT_SECONDS")
    ocr_enabled: bool = Field(default=True, alias="OCR_ENABLED")
    fts_language: str = Field(default="english", alias="FTS_LANGUAGE")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        """SQLAlchemy needs an async driver; accept the plain URL Render hands out."""
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql://", 1)
        if v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024



@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
