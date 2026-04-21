"""Render compose, write runtime config, run migrations."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
from importlib import resources
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from agentibrain.config import BrainSettings

COMPOSE_TEMPLATE = "compose.yml.j2"

# Defaults for the bundled stack — written to .env so `.env` is the single
# source of truth for the compose credentials.
DEFAULT_POSTGRES_PASSWORD = "agentibrain"
DEFAULT_MINIO_USER = "agentibrain"
DEFAULT_MINIO_PASSWORD = "agentibrain"


def _templates_dir() -> Path:
    return Path(__file__).parent / "templates" / "compose"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(_templates_dir()),
        autoescape=select_autoescape([]),
        keep_trailing_newline=True,
    )


def generate_token() -> str:
    """Random 32-byte URL-safe token for KB_ROUTER_TOKEN."""
    return secrets.token_urlsafe(32)


def render_compose(settings: BrainSettings) -> str:
    """Render the unified compose file from the Jinja template."""
    env = _env()
    tmpl = env.get_template(COMPOSE_TEMPLATE)
    return tmpl.render(
        storage_mode=settings.mode,
        vault_path=str(settings.vault_path.expanduser().resolve()),
        s3_bucket=settings.s3_bucket or "agentibrain-artifacts",
    )


def write_config(settings: BrainSettings) -> Path:
    """Persist settings to ``<config_dir>/config.yaml`` (minus secrets).

    Secrets live in ``<config_dir>/.env`` (chmod 600) so ``docker compose``
    picks them up. config.yaml stays world-readable with no sensitive fields.
    """
    cfg_dir = settings.config_dir.expanduser()
    cfg_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "mode": settings.mode,
        "vault_path": str(settings.vault_path),
        "s3_bucket": settings.s3_bucket,
        "s3_endpoint": settings.s3_endpoint,
        "brain_url": settings.brain_url,
        "llm_gateway_url": settings.llm_gateway_url,
        # postgres_url / redis_url are NOT written here — they may contain
        # passwords. If operators override them via flags, they come from env.
    }
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return cfg_path


def write_env_file(settings: BrainSettings, token: str) -> Path:
    """Persist runtime secrets + compose credentials to ``<config_dir>/.env``.

    This .env is the single source of truth for the stack — compose reads it
    via ``--env-file``. Includes generated defaults for bundled Postgres/MinIO
    so the friend never has to guess.
    """
    cfg_dir = settings.config_dir.expanduser()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    env_path = cfg_dir / ".env"

    lines: list[str] = [
        f"KB_ROUTER_TOKEN={token}",
        f"POSTGRES_PASSWORD={os.getenv('POSTGRES_PASSWORD', DEFAULT_POSTGRES_PASSWORD)}",
        "LOG_LEVEL=INFO",
    ]
    if settings.openai_api_key is not None:
        lines.append(f"OPENAI_API_KEY={settings.openai_api_key.get_secret_value()}")
    if settings.llm_gateway_url:
        lines.append(f"INFERENCE_URL={settings.llm_gateway_url}")
    if settings.mode == "local":
        lines.append(f"MINIO_ROOT_USER={os.getenv('MINIO_ROOT_USER', DEFAULT_MINIO_USER)}")
        lines.append(
            f"MINIO_ROOT_PASSWORD={os.getenv('MINIO_ROOT_PASSWORD', DEFAULT_MINIO_PASSWORD)}"
        )
    # ARTIFACT_STORE_URL is optional — binary ingest fails clearly when unset.
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)
    return env_path


def write_compose(settings: BrainSettings, rendered: str) -> Path:
    cfg_dir = settings.config_dir.expanduser()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "compose.yml"
    path.write_text(rendered)
    return path


def _docker_compose(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run ``docker compose``; fall back to ``docker-compose`` for older installs."""
    if shutil.which("docker"):
        proc = subprocess.run(
            ["docker", "compose", *cmd],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 127:
            return proc
    return subprocess.run(
        ["docker-compose", *cmd],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def compose_up(settings: BrainSettings) -> subprocess.CompletedProcess:
    cfg_dir = settings.config_dir.expanduser()
    return _docker_compose(["--env-file", ".env", "up", "-d"], cfg_dir)


def compose_down(settings: BrainSettings) -> subprocess.CompletedProcess:
    cfg_dir = settings.config_dir.expanduser()
    return _docker_compose(["--env-file", ".env", "down"], cfg_dir)


def compose_ps(settings: BrainSettings) -> subprocess.CompletedProcess:
    cfg_dir = settings.config_dir.expanduser()
    return _docker_compose(["ps"], cfg_dir)


def migrations_dir() -> Path:
    """Locate packaged SQL migrations via importlib.resources.

    Works in both editable installs and wheel installs because migrations/ is
    a package directory under agentibrain/ (declared in pyproject package-data).
    """
    try:
        root = resources.files("agentibrain") / "migrations"
        return Path(str(root))
    except (ModuleNotFoundError, AttributeError):
        # Defensive fallback — should not be hit in practice.
        return Path(__file__).resolve().parent / "migrations"


def _wait_for_postgres(dsn: str, *, max_attempts: int = 30, sleep_seconds: float = 2.0) -> bool:
    """Poll ``pg_isready`` until Postgres accepts connections or max_attempts."""
    pg_isready = shutil.which("pg_isready")
    if not pg_isready:
        return True  # Best-effort — fall through to psql and let it fail loudly if needed.
    for _ in range(max_attempts):
        proc = subprocess.run(
            [pg_isready, "-d", dsn],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return True
        time.sleep(sleep_seconds)
    return False


def run_migrations(settings: BrainSettings) -> list[str]:
    """Apply SQL migrations in order via ``psql``.

    Waits for Postgres readiness via ``pg_isready`` before running (compose
    exits as soon as containers start, not when services are accepting
    connections). Returns a list of human-readable status lines.

    Schema ownership moves to Alembic in a future release.
    """
    if not shutil.which("psql"):
        return ["psql not found on PATH — skipped migrations. Install postgresql-client."]

    dsn = settings.postgres_url or (
        f"postgresql://agentibrain:{os.getenv('POSTGRES_PASSWORD', DEFAULT_POSTGRES_PASSWORD)}"
        f"@localhost:5432/agentibrain"
    )

    if not _wait_for_postgres(dsn):
        return ["Postgres did not accept connections within 60s — skipped migrations."]

    results: list[str] = []
    for path in sorted(migrations_dir().glob("*.sql")):
        proc = subprocess.run(
            ["psql", dsn, "-f", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            results.append(f"✓ {path.name}")
        else:
            results.append(f"✗ {path.name}: {proc.stderr.strip()[:200]}")
    return results
