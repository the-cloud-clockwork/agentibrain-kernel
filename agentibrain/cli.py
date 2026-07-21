"""``brain`` CLI entry point."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click
import httpx
import yaml
from pydantic import SecretStr
from rich.console import Console

from agentibrain import __version__, bootstrap
from agentibrain import scaffold as _scaffold
from agentibrain.config import DEFAULT_CONFIG_DIR, DEFAULT_CONFIG_PATH, BrainSettings

console = Console()


def _load_settings() -> BrainSettings:
    """Load BrainSettings from ``~/.agentibrain/config.yaml`` plus env."""
    payload: dict[str, Any] = {}
    if DEFAULT_CONFIG_PATH.exists():
        raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text()) or {}
        payload = {k: v for k, v in raw.items() if v is not None}
    env_path = DEFAULT_CONFIG_DIR / ".env"
    env_file = str(env_path) if env_path.exists() else None
    return BrainSettings(**payload, _env_file=env_file)


@click.group()
@click.version_option(__version__, prog_name="brain")
def main() -> None:
    """agentibrain — standalone brain + KB kernel."""


@main.command()
@click.option("--vault", type=click.Path(), required=False, help="Path to the vault.")
@click.option("--local", "local_mode", is_flag=True, help="Use MinIO instead of S3.")
@click.option("--s3-bucket", help="S3 bucket name (required without --local).")
@click.option("--s3-endpoint", help="S3 endpoint override (e.g. for external MinIO).")
@click.option("--postgres-url", help="External Postgres DSN. Defaults to bundled.")
@click.option("--redis-url", help="External Redis URL. Defaults to bundled.")
@click.option("--openai-key", help="OpenAI API key.", envvar="OPENAI_API_KEY")
@click.option("--llm-gateway-url", help="Optional inference-gateway URL (operator path).")
def init(
    vault: str | None,
    local_mode: bool,
    s3_bucket: str | None,
    s3_endpoint: str | None,
    postgres_url: str | None,
    redis_url: str | None,
    openai_key: str | None,
    llm_gateway_url: str | None,
) -> None:
    """Initialize a new brain deployment (writes config + prepares compose)."""
    mode = "local" if local_mode else "s3"
    if not local_mode and not s3_bucket:
        console.print("[red]--s3-bucket required without --local[/red]")
        sys.exit(2)

    vault_path = Path(vault).expanduser().resolve() if vault else Path.home() / "agentibrain-vault"

    settings = BrainSettings(
        mode=mode,
        vault_path=vault_path,
        s3_bucket=s3_bucket,
        s3_endpoint=s3_endpoint,
        postgres_url=postgres_url,
        redis_url=redis_url,
        openai_api_key=SecretStr(openai_key) if openai_key else None,
        llm_gateway_url=llm_gateway_url,
        _env_file=None,
    )

    token = bootstrap.generate_token()

    cfg_path = bootstrap.write_config(settings)
    env_path = bootstrap.write_env_file(settings, token)
    rendered = bootstrap.render_compose(settings)
    compose_path = bootstrap.write_compose(settings, rendered)

    console.print(f"[green]✓[/green] config     → {cfg_path}")
    console.print(f"[green]✓[/green] env        → {env_path}  (chmod 600)")
    console.print(f"[green]✓[/green] compose    → {compose_path}")
    console.print(f"[green]✓[/green] vault path → {settings.vault_path}")
    console.print()
    console.print("[bold]KB_ROUTER_TOKEN[/bold] (save this):")
    console.print(f"  {token}")
    console.print()
    console.print(
        "Next: [cyan]brain up[/cyan] to start the stack, then [cyan]brain scaffold[/cyan]."
    )


@main.command("up")
def up_cmd() -> None:
    """Start the brain stack (docker compose up -d + migrations)."""
    settings = _load_settings()
    proc = bootstrap.compose_up(settings)
    if proc.returncode != 0:
        console.print(f"[red]compose up failed[/red]\n{proc.stderr}")
        sys.exit(proc.returncode)
    console.print(proc.stdout or "[green]compose up ok[/green]")
    console.print("\nRunning migrations…")
    for line in bootstrap.run_migrations(settings):
        console.print(f"  {line}")


@main.command("down")
def down_cmd() -> None:
    """Stop the brain stack (docker compose down)."""
    settings = _load_settings()
    proc = bootstrap.compose_down(settings)
    if proc.returncode != 0:
        console.print(f"[red]compose down failed[/red]\n{proc.stderr}")
        sys.exit(proc.returncode)
    console.print(proc.stdout or "[green]compose down ok[/green]")


@main.command("status")
def status_cmd() -> None:
    """Show health of all services."""
    settings = _load_settings()
    ps = bootstrap.compose_ps(settings)
    console.print("[bold]docker compose ps[/bold]")
    console.print(ps.stdout)

    token_path = settings.config_dir.expanduser() / ".env"
    token = None
    if token_path.exists():
        for line in token_path.read_text().splitlines():
            if line.startswith("KB_ROUTER_TOKEN="):
                token = line.split("=", 1)[1].strip()
                break

    if not token:
        console.print("[yellow]no KB_ROUTER_TOKEN — run `brain init` first[/yellow]")
        return

    try:
        r = httpx.get(
            f"{settings.brain_url}/health",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        console.print(f"GET {settings.brain_url}/health → {r.status_code}")
        if r.headers.get("content-type", "").startswith("application/json"):
            console.print(r.json())
    except httpx.HTTPError as e:
        console.print(f"[red]health check failed: {e}[/red]")


@main.command("check")
@click.option("--brain-url", envvar="BRAIN_URL", help="Override brain-api base URL.")
@click.option(
    "--token",
    envvar="KB_ROUTER_TOKEN",
    help="Bearer token (defaults to env / settings).",
)
def check_cmd(brain_url: str | None, token: str | None) -> None:
    """Deep sanity check — verify every dependency actually works.

    Calls brain-api /health/deep, which round-trips a vault write, asks the
    embeddings service to hit its DB and run a real embedding call (checking
    the model's output dimension against the pgvector schema), and verifies
    the inference gateway accepts the configured key.

    Exit 0 when everything passes, 1 when any check is degraded.
    """
    settings = _load_settings()
    base = (brain_url or settings.brain_url).rstrip("/")

    if not token:
        env_path = settings.config_dir.expanduser() / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("KB_ROUTER_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    break
    if not token:
        console.print("[red]no KB_ROUTER_TOKEN — set env var or run `brain init`[/red]")
        sys.exit(2)

    try:
        r = httpx.get(
            f"{base}/health/deep",
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        console.print(f"[red]GET {base}/health/deep failed: {e}[/red]")
        sys.exit(1)

    payload = r.json()
    overall = payload.get("status", "unknown")
    checks = payload.get("checks", {})

    for name, detail in checks.items():
        mark = "[green]✓[/green]" if detail.get("ok") else "[red]✗[/red]"
        console.print(f"{mark} [bold]{name}[/bold]")
        for key, value in detail.items():
            if key == "ok":
                continue
            if key == "checks" and isinstance(value, dict):
                for sub_name, sub in value.items():
                    sub_mark = "[green]✓[/green]" if sub.get("ok") else "[red]✗[/red]"
                    sub_detail = " ".join(
                        f"{k}={v}" for k, v in sub.items() if k != "ok"
                    )
                    console.print(f"    {sub_mark} {sub_name}: {sub_detail}")
                continue
            console.print(f"    {key}: {value}")

    if overall == "ok":
        console.print("[green]all checks passed[/green]")
        sys.exit(0)
    console.print(f"[red]status: {overall}[/red]")
    sys.exit(1)


@main.command("tick")
@click.option("--dry-run", is_flag=True, help="Run tick read-only (no writes).")
@click.option("--no-ai", is_flag=True, help="Skip AI reasoning phase (deterministic only).")
@click.option("--wait", is_flag=True, help="Poll until the job completes.")
@click.option("--brain-url", envvar="BRAIN_URL", help="Override brain-api base URL.")
@click.option(
    "--token",
    envvar="KB_ROUTER_TOKEN",
    help="Bearer token (defaults to env / settings).",
)
def tick_cmd(
    dry_run: bool,
    no_ai: bool,
    wait: bool,
    brain_url: str | None,
    token: str | None,
) -> None:
    """Trigger a manual brain tick via the /tick endpoint.

    Enqueues a request file in brain-feed/ticks/requested/ which the
    tick-cron drains within ~2 minutes. Use --wait to block until completion.
    """
    settings = _load_settings()
    base = (brain_url or settings.brain_url).rstrip("/")

    if not token:
        env_path = settings.config_dir.expanduser() / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("KB_ROUTER_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    break
    if not token:
        console.print("[red]no KB_ROUTER_TOKEN — set env var or run `brain init`[/red]")
        sys.exit(2)

    headers = {"Authorization": f"Bearer {token}"}
    params = {"dry_run": str(dry_run).lower(), "no_ai": str(no_ai).lower(), "source": "cli"}

    try:
        r = httpx.post(f"{base}/tick", headers=headers, params=params, timeout=10.0)
        r.raise_for_status()
    except httpx.HTTPError as e:
        console.print(f"[red]POST /tick failed: {e}[/red]")
        sys.exit(1)

    job = r.json()
    job_id = job.get("job_id", "?")
    console.print(f"[green]✓[/green] tick enqueued — job_id={job_id}")

    if not wait:
        console.print(
            f"  poll: [cyan]curl -H 'Authorization: Bearer …' {base}/tick/{job_id}[/cyan]"
        )
        return

    console.print("  waiting (≤5 min)…")
    import time as _time

    deadline = _time.time() + 300
    while _time.time() < deadline:
        try:
            s = httpx.get(f"{base}/tick/{job_id}", headers=headers, timeout=10.0)
            s.raise_for_status()
            status = s.json()
        except httpx.HTTPError:
            _time.sleep(2)
            continue

        state = status.get("status")
        if state in {"completed", "failed"}:
            console.print(f"  [bold]{state}[/bold]")
            console.print(status)
            sys.exit(0 if state == "completed" else 1)
        _time.sleep(3)

    console.print("[yellow]timeout — job still running. Check tick-cron logs.[/yellow]")
    sys.exit(2)


@main.command("scaffold")
@click.argument("vault_path", type=click.Path(), required=False)
@click.option("--force-upgrade", is_flag=True, help="Overwrite existing .brain-schema.")
def scaffold_cmd(vault_path: str | None, force_upgrade: bool) -> None:
    """Seed the vault folder layout."""
    if vault_path is None:
        settings = _load_settings()
        path = settings.vault_path
    else:
        path = Path(vault_path).expanduser().resolve()

    try:
        result = _scaffold.scaffold(path, force_upgrade=force_upgrade)
    except _scaffold.SchemaConflict as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(2)

    console.print(f"[green]✓[/green] vault    → {result['vault']}")
    console.print(f"[green]✓[/green] created  → {result['folders_created']} new folders")
    console.print(f"[green]✓[/green] seeded   → {result['files_written']} files")
    console.print(
        f"[green]✓[/green] schema   → v{result['schema']['version']} ({result['schema']['schema']})"
    )


@main.command("version")
def version_cmd() -> None:
    """Print the kernel version."""
    console.print(__version__)


if __name__ == "__main__":
    main()
