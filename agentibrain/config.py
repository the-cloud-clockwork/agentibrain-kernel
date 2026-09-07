"""Kernel configuration schema.

Loaded from ``~/.agentibrain/config.yaml`` (written by ``agentibrain init``) plus
environment variables. Environment wins over the file so operators can override
in CI / K8s without rewriting the config file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_DIR = Path.home() / ".agentibrain"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.yaml"


class BrainSettings(BaseSettings):
    """Runtime settings for the agentibrain kernel.

    Two storage modes:
    - ``mode="s3"``  — use AWS S3 (``s3_bucket`` + AWS creds from env)
    - ``mode="local"`` — use MinIO bundled with the compose stack
    """

    model_config = SettingsConfigDict(
        env_prefix="BRAIN_",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Storage mode ---
    mode: Literal["s3", "local"] = "local"

    # --- Paths ---
    vault_path: Path = Field(
        default=Path.home() / "agentibrain-vault",
        description="Filesystem path to the Obsidian-compatible vault.",
    )
    config_dir: Path = Field(
        default=DEFAULT_CONFIG_DIR,
        description="Where rendered compose + state live.",
    )

    # --- Storage: S3 / MinIO ---
    s3_bucket: str | None = Field(
        default=None,
        description="S3 bucket name. Required when mode='s3'.",
    )
    s3_endpoint: str | None = Field(
        default=None,
        description="S3 endpoint URL. Set to the MinIO endpoint when mode='local'.",
    )
    s3_region: str = Field(default="us-east-1")

    # --- Databases ---
    postgres_url: str | None = Field(
        default=None,
        description=(
            "Postgres DSN. When None, the bundled Postgres in compose is used and a "
            "DSN is generated at init time."
        ),
    )
    redis_url: str | None = Field(
        default=None,
        description="Redis URL. When None, the bundled Redis is used.",
    )

    # --- External credentials (SecretStr — never logged) ---
    openai_api_key: SecretStr | None = Field(default=None)
    kb_router_token: SecretStr | None = Field(
        default=None,
        description="Bearer token for kernel HTTP API. Generated at init if absent.",
    )

    # --- Optional inference gateway (operator path) ---
    llm_gateway_url: str | None = Field(
        default=None,
        description=(
            "If set, kb-router routes LLM classification calls through this gateway "
            "instead of directly to OpenAI. Operator path."
        ),
    )

    # --- Bundled local inference (Ollama) ---
    ollama: bool = Field(
        default=False,
        description=(
            "Render a bundled Ollama and point both chat and embeddings at it, "
            "so the stack needs no API key at all."
        ),
    )
    ollama_chat_model: str = Field(default="llama3.2:3b")
    ollama_embed_model: str = Field(default="nomic-embed-text")

    # --- Kernel HTTP API ---
    port_brain_api: int = Field(
        default=8103,
        validation_alias=AliasChoices("PORT_BRAIN_API", "BRAIN_PORT_BRAIN_API"),
        description="Published brain-api port (compose PORT_BRAIN_API).",
    )
    brain_url: str = Field(
        default="",
        description=(
            "Externally-visible URL of brain-api. Empty derives "
            "http://localhost:{port_brain_api}, so local stacks need no flag."
        ),
    )

    @model_validator(mode="after")
    def _derive_brain_url(self) -> BrainSettings:
        if not self.brain_url:
            self.brain_url = f"http://localhost:{self.port_brain_api}"
        return self

    def require_s3(self) -> None:
        """Raise if mode='s3' but bucket is missing."""
        if self.mode == "s3" and not self.s3_bucket:
            raise ValueError("mode='s3' requires s3_bucket to be set")
