"""Render compose, write runtime config, run migrations."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from agentibrain.config import BrainSettings

COMPOSE_TEMPLATE = "compose.yml.j2"


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

    Secrets live in ``<config_dir>/.env`` so ``docker compose`` picks them up.
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
    }
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return cfg_path


def write_env_file(settings: BrainSettings, token: str) -> Path:
    cfg_dir = settings.config_dir.expanduser()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    env_path = cfg_dir / ".env"
    lines = [f"KB_ROUTER_TOKEN={token}"]
    if settings.openai_api_key is not None:
        lines.append(f"OPENAI_API_KEY={settings.openai_api_key.get_secret_value()}")
    if settings.llm_gateway_url:
        lines.append(f"INFERENCE_URL={settings.llm_gateway_url}")
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
    """Return the repo-local ``migrations/`` directory.

    When installed via pip, the CLI ships migrations as package data; for this
    early cut we resolve relative to the kernel source tree. Phase 7 hardening.
    """
    here = Path(__file__).resolve()
    # In an editable install, migrations/ lives next to agentibrain/.
    candidate = here.parent.parent / "migrations"
    if candidate.is_dir():
        return candidate
    return here.parent / "migrations"


def run_migrations(settings: BrainSettings) -> list[str]:
    """Apply SQL migrations in order via ``psql``.

    Prints a note and skips if ``psql`` isn't on PATH. Phase 5 ships a minimal
    migration set; schema ownership moves to Alembic in Phase 7.
    """
    if not shutil.which("psql"):
        return ["psql not found on PATH — skipped migrations. Install postgresql-client."]

    dsn = settings.postgres_url or (
        f"postgresql://agentibrain:{os.getenv('POSTGRES_PASSWORD', 'agentibrain')}"
        f"@localhost:5432/agentibrain"
    )
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
