"""Render compose, write runtime config, run migrations."""

from __future__ import annotations

import os
import platform
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
        ollama=settings.ollama,
        ollama_chat_model=settings.ollama_chat_model,
        ollama_embed_model=settings.ollama_embed_model,
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
    # brain_url auto-derives from port_brain_api when unset. Persisting the
    # derived literal would freeze it in config.yaml and silently mask any
    # later PORT_BRAIN_API override — keep only an explicit operator URL.
    if settings.brain_url == f"http://localhost:{settings.port_brain_api}":
        payload.pop("brain_url")
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return cfg_path


def _existing_assignments(env_path: Path) -> dict[str, str]:
    """Active ``KEY=value`` lines already in the file. Comments are ignored, so
    a commented placeholder never counts as a value the operator chose."""
    if not env_path.exists():
        return {}
    found: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        found[key.strip()] = value
    return found


def write_env_file(settings: BrainSettings, token: str) -> Path:
    """Persist runtime secrets + compose credentials to ``<config_dir>/.env``.

    This .env is the single source of truth for the stack — compose reads it
    via ``--env-file``. Includes generated defaults for bundled Postgres/MinIO
    so first-run users never have to guess.

    Re-running init MUST NOT cost the operator their configuration. Any key
    already assigned in the file keeps its value: a rotated KB_ROUTER_TOKEN
    stays rotated, hand-set provider keys survive, and only names absent from
    the file are added. Nothing is ever removed. An explicit flag
    (``--openai-key``, ``--llm-gateway-url``) is the one thing that overrides,
    because the operator just typed it.
    """
    cfg_dir = settings.config_dir.expanduser()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    env_path = cfg_dir / ".env"
    existing = _existing_assignments(env_path)

    embeddings_key = existing.get("EMBEDDINGS_API_KEY") or generate_token()
    generated: dict[str, str] = {
        "KB_ROUTER_TOKEN": token,
        "EMBEDDINGS_API_KEY": embeddings_key,
        "EMBEDDINGS_API_KEYS": embeddings_key,
        "POSTGRES_PASSWORD": os.getenv("POSTGRES_PASSWORD", DEFAULT_POSTGRES_PASSWORD),
        "LOG_LEVEL": "INFO",
    }
    if settings.mode == "local":
        generated["MINIO_ROOT_USER"] = os.getenv("MINIO_ROOT_USER", DEFAULT_MINIO_USER)
        generated["MINIO_ROOT_PASSWORD"] = os.getenv("MINIO_ROOT_PASSWORD", DEFAULT_MINIO_PASSWORD)

    # Typed this run, so it wins over whatever the file holds.
    explicit: dict[str, str] = {}
    if settings.openai_api_key is not None:
        # LLM_API_KEY is what the embeddings service reads; OPENAI_API_KEY is
        # read by nothing in the stack.
        secret = settings.openai_api_key.get_secret_value()
        explicit["LLM_API_KEY"] = secret
        explicit["INFERENCE_API_KEY"] = secret
    if settings.llm_gateway_url:
        explicit["INFERENCE_URL"] = settings.llm_gateway_url

    resolved = {**generated, **existing, **explicit}

    lines = [f"{key}={value}" for key, value in resolved.items()]
    lines += _commented_settings(settings, offered=set(resolved))
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)
    return env_path


# Everything the rendered compose reads but init does not set. Emitted
# commented-out with its effective default, so the file itself says which
# names exist — a .env holding only generated secrets tells an operator
# nothing about what they are allowed to configure.
_OPTIONAL_ENV: tuple[tuple[str, str, str], ...] = (
    ("LLM_API_KEY", "", "embeddings: key for the embedding provider"),
    ("LLM_API_BASE", "", "embeddings: OpenAI-compatible base URL"),
    ("LLM_EMBED_MODEL", "text-embedding-3-small", "embeddings: model"),
    ("EMBED_DIM", "", "embeddings: pin the vector width for an unrecognised model"),
    ("INFERENCE_URL", "", "AI tick + kb_brief: OpenAI-compatible base URL"),
    ("INFERENCE_API_KEY", "", "AI tick + kb_brief: key"),
    ("BRAIN_CLASSIFY_MODEL", "brain-classify", "model name brain-api sends"),
    ("BRAIN_BRIEF_MODEL", "brain-brief", "model name the tick and mcp send"),
    ("EMBEDDINGS_API_KEY", "", "brain-api → embeddings auth; pairs with EMBEDDINGS_API_KEYS"),
    ("EMBEDDINGS_API_KEYS", "", "embeddings: accepted keys, empty means unauthenticated"),
    ("TICK_INTERVAL_SECONDS", "7200", "scheduled tick cadence"),
    ("TICK_DRAIN_INTERVAL_SECONDS", "30", "on-demand tick poll interval"),
    ("BRAIN_LLM_TIMEOUT_SECONDS", "600", "deadline for the AI synthesis call"),
    ("ARTIFACT_STORE_URL", "", "optional; binary ingest fails clearly when unset"),
)


def _commented_settings(settings: BrainSettings, offered: set[str] | None = None) -> list[str]:
    """The optional block appended to .env, minus anything already assigned."""
    written = set(offered or ())
    if settings.ollama:
        # The compose defaults already point these at the bundled Ollama;
        # uncommenting a blank here would override them back to nothing.
        written |= {
            "LLM_API_KEY",
            "LLM_API_BASE",
            "LLM_EMBED_MODEL",
            "EMBED_DIM",
            "INFERENCE_URL",
            "BRAIN_CLASSIFY_MODEL",
            "BRAIN_BRIEF_MODEL",
        }
    out = ["", "# --- optional: uncomment to change ---"]
    for name, default, note in _OPTIONAL_ENV:
        if name in written:
            continue
        out.append(f"# {note}")
        out.append(f"#{name}={default}")
    return out


def write_compose(settings: BrainSettings, rendered: str) -> Path:
    cfg_dir = settings.config_dir.expanduser()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "compose.yml"
    path.write_text(rendered)
    return path


# Any kernel root-compose file names this container; used to recognise a
# checkout when walking up from cwd.
COMPOSE_MARKER = "agentibrain_brain_api"


def _read_env_value(env_path: Path, key: str) -> str:
    if not env_path.exists():
        return ""
    for line in env_path.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def find_deployment(settings: BrainSettings, cwd: Path | None = None) -> tuple[str, Path] | None:
    """Locate the compose deployment the CLI should drive.

    Returns ``(mode, compose_dir)`` — mode is ``"root-compose"`` (repo
    checkout managed by local/bootstrap.sh) or ``"init"`` (stack rendered by
    ``agentibrain init``) — or None when no deployment exists.

    Order: the checkout you are standing in wins — running a command from
    inside checkout B must never target checkout A that an old bootstrap
    pinned. The AGENTIBRAIN_REPO pin (written by local/bootstrap.sh into
    ~/.agentibrain/.env) covers every other cwd; the init-rendered stack
    comes last.
    """
    cfg_dir = settings.config_dir.expanduser()

    start = (cwd or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        compose = candidate / "compose.yml"
        try:
            if compose.is_file() and COMPOSE_MARKER in compose.read_text():
                return ("root-compose", candidate)
        except OSError:
            continue

    repo = _read_env_value(cfg_dir / ".env", "AGENTIBRAIN_REPO")
    if repo:
        repo_dir = Path(repo).expanduser()
        if (repo_dir / "compose.yml").is_file():
            return ("root-compose", repo_dir)

    if (cfg_dir / "compose.yml").is_file():
        return ("init", cfg_dir)
    return None


def deployment_env_path(settings: BrainSettings, deployment: tuple[str, Path] | None) -> Path:
    """The .env the discovered deployment actually reads.

    A repo checkout reads its own; only the init-rendered stack reads the one
    under config_dir. The two are usually the same file — local/bootstrap.sh
    symlinks them — but nothing guarantees it, and a second checkout breaks it.
    """
    cfg_env = settings.config_dir.expanduser() / ".env"
    if deployment is None:
        return cfg_env
    mode, compose_dir = deployment
    return compose_dir / ".env" if mode == "root-compose" else cfg_env


def resolve_endpoint(
    settings: BrainSettings, deployment: tuple[str, Path] | None
) -> tuple[str, str]:
    """``(brain_url, token)`` as the deployment on this machine defines them.

    Both values are already on disk next to the compose file that publishes the
    port — asking the operator to supply either for a local stack is asking
    them to retype what the machine knows. Falls back to config_dir's .env for
    the token so a checkout that keeps secrets there still resolves.
    """
    env_path = deployment_env_path(settings, deployment)
    token = _read_env_value(env_path, "KB_ROUTER_TOKEN")
    if not token:
        token = _read_env_value(settings.config_dir.expanduser() / ".env", "KB_ROUTER_TOKEN")
    port = _read_env_value(env_path, "PORT_BRAIN_API")
    url = f"http://localhost:{port}" if port.isdigit() else settings.brain_url
    return url, token


def _compose_binargs() -> list[str]:
    """Pick `docker compose` vs legacy `docker-compose`, probing the plugin.

    `docker compose` on a plugin-less install exits 1 (not 127), so a
    which("docker") test alone would pick a broken invocation while the
    legacy binary sits unused — the same fallback _docker_compose does
    reactively, done proactively here for streaming commands.
    """
    if shutil.which("docker"):
        probe = subprocess.run(
            ["docker", "compose", "version"],
            check=False,
            capture_output=True,
        )
        if probe.returncode == 0:
            return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return ["docker", "compose"]  # fail loudly with docker's own message


def compose_stream(cmd: list[str], cwd: Path) -> int:
    """Run ``docker compose`` with inherited stdio for long/streaming commands
    (build, logs -f) so output reaches the terminal live."""
    return subprocess.run([*_compose_binargs(), *cmd], cwd=cwd, check=False).returncode


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
        hint = (
            "brew install libpq && brew link --force libpq"
            if platform.system() == "Darwin"
            else "apt install postgresql-client"
        )
        return [f"psql not found on PATH — skipped migrations. Install it: {hint}."]

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
