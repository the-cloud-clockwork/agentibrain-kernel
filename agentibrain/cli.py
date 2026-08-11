"""``agentibrain`` CLI entry point."""

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
@click.version_option(__version__, prog_name="agentibrain")
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
        "Next: [cyan]agentibrain up[/cyan] to start the stack, then [cyan]agentibrain scaffold[/cyan]."
    )


def _find_deployment_or_exit() -> tuple[str, Path, BrainSettings]:
    """Detect the compose deployment (any mode, any cwd) or exit 2."""
    settings = _load_settings()
    dep = bootstrap.find_deployment(settings)
    if dep is None:
        console.print(
            "[red]no agentibrain deployment found — run ./local/bootstrap.sh "
            "in the repo, or `agentibrain init`[/red]"
        )
        sys.exit(2)
    mode, compose_dir = dep
    return mode, compose_dir, settings


@main.command("up")
def up_cmd() -> None:
    """Start the brain stack wherever it lives (docker compose up -d)."""
    mode, compose_dir, settings = _find_deployment_or_exit()
    if mode == "init":
        proc = bootstrap.compose_up(settings)
        if proc.returncode != 0:
            console.print(f"[red]compose up failed[/red]\n{proc.stderr}")
            sys.exit(proc.returncode)
        console.print(proc.stdout or "[green]compose up ok[/green]")
        console.print("\nRunning migrations…")
        for line in bootstrap.run_migrations(settings):
            console.print(f"  {line}")
        return
    console.print(f"[bold]starting[/bold] ({mode} @ {compose_dir})")
    rc = bootstrap.compose_stream(["up", "-d"], compose_dir)
    if rc != 0:
        sys.exit(rc)


@main.command("build")
@click.argument("services", nargs=-1)
def build_cmd(services: tuple[str, ...]) -> None:
    """Rebuild + restart the stack (docker compose up -d --build [SERVICES]).

    The one command that makes a code change take effect: finds the compose
    deployment, rebuilds changed images, recreates their containers, then
    shows the resulting ps.
    """
    _, compose_dir, _ = _find_deployment_or_exit()
    console.print(f"[bold]build + up[/bold] @ {compose_dir}")
    rc = bootstrap.compose_stream(["up", "-d", "--build", *services], compose_dir)
    if rc != 0:
        sys.exit(rc)
    ps = bootstrap._docker_compose(["ps"], compose_dir)
    console.print(ps.stdout)


@main.command("logs")
@click.argument("service", required=False)
@click.option("-f", "--follow", is_flag=True, help="Stream logs until Ctrl-C.")
@click.option("--since", default=None, help="Only logs newer than this (e.g. 10m, 2h).")
@click.option("--tail", default=None, type=int, help="Number of trailing lines per service.")
def logs_cmd(service: str | None, follow: bool, since: str | None, tail: int | None) -> None:
    """Show service logs (docker compose logs passthrough)."""
    _, compose_dir, _ = _find_deployment_or_exit()
    args = ["logs"]
    if follow:
        args.append("-f")
    if since:
        args += ["--since", since]
    if tail is not None:
        args += ["--tail", str(tail)]
    if service:
        args.append(service)
    sys.exit(bootstrap.compose_stream(args, compose_dir))


@main.command("down")
def down_cmd() -> None:
    """Stop the brain stack (docker compose down — volumes survive)."""
    mode, compose_dir, settings = _find_deployment_or_exit()
    if mode == "init":
        proc = bootstrap.compose_down(settings)
    else:
        proc = bootstrap._docker_compose(["down"], compose_dir)
    if proc.returncode != 0:
        console.print(f"[red]compose down failed[/red]\n{proc.stderr}")
        sys.exit(proc.returncode)
    console.print(proc.stdout or "[green]compose down ok[/green]")


@main.command("status")
def status_cmd() -> None:
    """Show health of all services."""
    settings = _load_settings()
    dep = bootstrap.find_deployment(settings)
    if dep:
        mode, compose_dir = dep
        ps = bootstrap._docker_compose(["ps"], compose_dir)
        console.print(f"[bold]docker compose ps[/bold] ({mode} @ {compose_dir})")
        console.print(ps.stdout)
    else:
        # The HTTP health check below still runs.
        console.print(
            "[yellow]no deployment found — run ./local/bootstrap.sh in the repo, "
            "or `agentibrain init`[/yellow]"
        )

    token_path = settings.config_dir.expanduser() / ".env"
    token = None
    if token_path.exists():
        for line in token_path.read_text().splitlines():
            if line.startswith("KB_ROUTER_TOKEN="):
                token = line.split("=", 1)[1].strip()
                break

    if not token:
        console.print("[yellow]no KB_ROUTER_TOKEN — run `agentibrain init` first[/yellow]")
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
        console.print("[red]no KB_ROUTER_TOKEN — set env var or run `agentibrain init`[/red]")
        sys.exit(2)

    try:
        r = httpx.get(
            f"{base}/health/deep",
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        # A degraded endpoint returns 500-with-JSON in some deployments; try to
        # render its body before giving up so the operator sees the reason.
        body = None
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                body = resp.json()
            except ValueError:
                body = None
        console.print(f"[red]GET {base}/health/deep failed: {e}[/red]")
        if isinstance(body, dict) and body.get("detail"):
            console.print(f"[red]  {body['detail']}[/red]")
        sys.exit(1)

    try:
        payload = r.json()
    except ValueError:
        console.print(f"[red]non-JSON response from {base}/health/deep:[/red]")
        console.print(r.text[:500])
        sys.exit(1)

    overall = payload.get("status", "unknown")
    checks = payload.get("checks", {})

    for name, detail in checks.items():
        if not isinstance(detail, dict):
            console.print(f"[red]✗[/red] [bold]{name}[/bold]: {detail}")
            continue
        mark = "[green]✓[/green]" if detail.get("ok") else "[red]✗[/red]"
        console.print(f"{mark} [bold]{name}[/bold]")
        for key, value in detail.items():
            if key == "ok":
                continue
            if key == "checks" and isinstance(value, dict):
                for sub_name, sub in value.items():
                    if not isinstance(sub, dict):
                        console.print(f"    {sub_name}: {sub}")
                        continue
                    sub_mark = "[green]✓[/green]" if sub.get("ok") else "[red]✗[/red]"
                    sub_detail = " ".join(f"{k}={v}" for k, v in sub.items() if k != "ok")
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
    """Trigger a manual agentibrain tick via the /tick endpoint.

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
        console.print("[red]no KB_ROUTER_TOKEN — set env var or run `agentibrain init`[/red]")
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


def _drain_marker_dir(
    directory: Path, base: str, headers: dict, verbose: bool = False
) -> dict[str, int]:
    """Replay buffered marker files as POST /marker; delete each on success.

    Idempotency-key parity with agentihooks (uuid5 of session-type-content) so
    replays dedupe server-side; the original `ts` rides in attrs so brain-api
    backdates the marker into its original dated files. Unparseable or
    payload-rejected (400/404/422) files quarantine as .bad; transient
    failures (network, 5xx, 401/403/429) stay put for the next sync.
    """
    import json as _json
    import uuid as _uuid

    stats = {"drained": 0, "quarantined": 0, "failed": 0}
    if not directory.is_dir():
        return stats
    files = sorted(directory.glob("*.json"))
    if verbose and files:
        console.print(f"  {directory.name}: {len(files)} buffered file(s) to replay")
    for i, f in enumerate(files):
        if verbose and i and i % 100 == 0:
            console.print(
                f"  {directory.name}: {i}/{len(files)} — "
                f"drained={stats['drained']} quarantined={stats['quarantined']} "
                f"failed={stats['failed']}"
            )
        try:
            entry = _json.loads(f.read_text(encoding="utf-8"))
            session_id = entry.get("session_id") or ""
            content = (entry.get("content") or "")[:4096]
            marker_type = entry.get("type") or ""
            if not marker_type or not content.strip():
                raise ValueError("missing type/content")
        except (ValueError, OSError, TypeError):
            try:
                f.rename(f.with_suffix(".bad"))
                stats["quarantined"] += 1
            except OSError:
                pass
            continue

        attrs = dict(entry.get("attrs") or {})
        attrs.setdefault("session_id", session_id)
        attrs.setdefault("source", entry.get("agent_name") or attrs.get("source") or "sync")
        if entry.get("project"):
            attrs.setdefault("project", entry["project"])
        if entry.get("ts"):
            attrs.setdefault("ts", entry["ts"])
        idem = _uuid.uuid5(_uuid.NAMESPACE_URL, f"{session_id}-{marker_type}-{content}").hex[:32]

        try:
            r = httpx.post(
                f"{base}/marker",
                headers={**headers, "X-Idempotency-Key": idem},
                json={"type": marker_type, "content": content, "attrs": attrs},
                timeout=15.0,
            )
        except httpx.HTTPError:
            stats["failed"] += 1
            continue
        if r.status_code in (400, 404, 422):
            # Payload-level rejection — permanent. 401/403/429 (stale token,
            # rate limit) must stay retry-eligible or a misconfigured token
            # destroys the entire queue in one pass.
            try:
                f.rename(f.with_suffix(".bad"))
                stats["quarantined"] += 1
            except OSError:
                pass
            continue
        if r.status_code >= 400:
            stats["failed"] += 1
            continue

        try:
            f.unlink()
        except FileNotFoundError:
            pass  # a concurrent drain won this file
        stats["drained"] += 1
    return stats


@main.command("sync")
@click.option("--wait", is_flag=True, help="Poll until the follow-up tick completes.")
@click.option(
    "--check",
    "check",
    is_flag=True,
    help="Like --wait, but narrates progress: drain counters, tick state changes, final verdict.",
)
@click.option("--brain-url", envvar="BRAIN_URL", help="Override brain-api base URL.")
@click.option(
    "--token",
    envvar="KB_ROUTER_TOKEN",
    help="Bearer token (defaults to env / settings).",
)
def sync_cmd(wait: bool, check: bool, brain_url: str | None, token: str | None) -> None:
    """Re-ingest everything into the brain.

    Replays the agentihooks marker buffers (brain-outbox and its -backlog
    sibling) over POST /marker, then requests a tick so replayed markers
    cluster and the raw/ ingest index refreshes. Fully idempotent — safe to
    run any time.
    """
    import os as _os

    wait = wait or check
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
        console.print("[red]no KB_ROUTER_TOKEN — set env var or run `agentibrain init`[/red]")
        sys.exit(2)
    headers = {"Authorization": f"Bearer {token}"}

    outbox = Path(
        _os.environ.get(
            "BRAIN_WRITER_OUTBOX",
            str(Path.home() / ".agentihooks" / "brain-outbox"),
        )
    ).expanduser()
    backlog = outbox.with_name(outbox.name + "-backlog")

    totals = {"drained": 0, "quarantined": 0, "failed": 0}
    # Backlog first: its files are months older than the live outbox, and
    # append-only targets (BLOCKS.md) keep arrival order — replaying oldest
    # first keeps them chronological.
    for d in (backlog, outbox):
        n_buffered = len(list(d.glob("*.json"))) if d.is_dir() else 0
        if n_buffered == 0:
            console.print(f"  {d.name}: empty — nothing to replay")
            continue
        st = _drain_marker_dir(d, base, headers, verbose=check)
        for k, v in st.items():
            totals[k] += v
        console.print(
            f"  {d.name}: drained={st['drained']} "
            f"quarantined={st['quarantined']} failed={st['failed']}"
        )
    if totals["failed"]:
        console.print(
            f"[yellow]{totals['failed']} marker(s) could not be delivered — "
            "left in place for the next sync[/yellow]"
        )

    try:
        r = httpx.post(f"{base}/tick", headers=headers, params={"source": "sync"}, timeout=10.0)
        r.raise_for_status()
    except httpx.HTTPError as e:
        console.print(f"[red]POST /tick failed: {e}[/red]")
        sys.exit(1)
    job_id = r.json().get("job_id", "?")
    console.print(f"[green]✓[/green] sync tick enqueued — job_id={job_id}")

    # Exit contract: 0 = clean, 1 = hard failure (tick unreachable/failed),
    # 2 = degraded (some markers still buffered — rerun sync later).
    if not wait:
        sys.exit(2 if totals["failed"] else 0)

    console.print("  waiting (≤5 min)…")
    import time as _time

    deadline = _time.time() + 300
    started = _time.time()
    last_state = ""
    stall_hinted = False
    while _time.time() < deadline:
        try:
            s = httpx.get(f"{base}/tick/{job_id}", headers=headers, timeout=10.0)
            s.raise_for_status()
            status = s.json()
        except httpx.HTTPError:
            _time.sleep(2)
            continue
        state = status.get("status")
        if check and state and state != last_state:
            console.print(f"  tick {job_id}: [bold]{state}[/bold]")
            last_state = state
        if (
            check
            and not stall_hinted
            and state in {"pending", "requested", None}
            and (_time.time() - started) > 75
        ):
            stall_hinted = True
            console.print(
                "  [yellow]still pending after 75s — tick-drain normally picks jobs up "
                "within ~30s. Is the stack current and running? Try "
                "`agentibrain status` and `agentibrain logs tick-drain --since 5m`; "
                "an old image needs `agentibrain build`.[/yellow]"
            )
        if state in {"completed", "failed"}:
            if not check:
                console.print(f"  [bold]{state}[/bold]")
            if check:
                detail = {
                    k: v
                    for k, v in status.items()
                    if k not in {"status", "job_id"} and v not in (None, "", {})
                }
                error_tail = detail.pop("error_tail", None)
                if detail:
                    console.print(f"  tick detail: {detail}")
                if error_tail:
                    console.print("[red]tick error tail:[/red]")
                    console.print(error_tail)
                console.print(
                    f"[bold]sync summary[/bold]: replayed={totals['drained']} "
                    f"quarantined={totals['quarantined']} "
                    f"still-buffered={totals['failed']} tick={state}"
                )
                remaining = sum(
                    1 for d in (backlog, outbox) if d.is_dir() for _ in d.glob("*.json")
                )
                console.print(f"  buffers now hold {remaining} file(s)")
            if state != "completed":
                sys.exit(1)
            sys.exit(2 if totals["failed"] else 0)
        _time.sleep(3)

    console.print("[yellow]timeout — job still running. Check tick-drain logs.[/yellow]")
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
