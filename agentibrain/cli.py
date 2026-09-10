"""``agentibrain`` CLI entry point."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
import httpx
import yaml
from pydantic import SecretStr
from rich.console import Console
from rich.markup import escape

from agentibrain import __version__, bootstrap
from agentibrain import scaffold as _scaffold
from agentibrain.config import DEFAULT_CONFIG_DIR, DEFAULT_CONFIG_PATH, BrainSettings

console = Console()

# How long `tick --wait` / `sync --check` block before calling a tick stalled.
# MUST exceed brain-ops' BRAIN_LLM_TIMEOUT_SECONDS (600s) plus the drain's
# pickup interval, or a perfectly healthy slow tick reports as a timeout and
# sends the operator hunting for a fault that is not there.
TICK_WAIT_SECONDS = int(os.getenv("AGENTIBRAIN_TICK_WAIT_SECONDS", "900"))
# How often `tick --wait` says it is still alive while the model thinks.
_TICK_HEARTBEAT_SECONDS = 60


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
@click.option(
    "--ollama",
    "use_ollama",
    is_flag=True,
    help="Bundle Ollama and point chat + embeddings at it. No API key needed.",
)
def init(
    vault: str | None,
    local_mode: bool,
    s3_bucket: str | None,
    s3_endpoint: str | None,
    postgres_url: str | None,
    redis_url: str | None,
    openai_key: str | None,
    llm_gateway_url: str | None,
    use_ollama: bool,
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
        ollama=use_ollama,
        _env_file=None,
    )

    token = bootstrap.generate_token()

    settings.vault_path.mkdir(parents=True, exist_ok=True)
    cfg_path = bootstrap.write_config(settings)
    env_path = bootstrap.write_env_file(settings, token)
    rendered = bootstrap.render_compose(settings)
    compose_path = bootstrap.write_compose(settings, rendered)

    console.print(f"[green]✓[/green] config     → {cfg_path}")
    console.print(f"[green]✓[/green] env        → {env_path}  (chmod 600)")
    console.print(f"[green]✓[/green] compose    → {compose_path}")
    console.print(f"[green]✓[/green] vault path → {settings.vault_path}")
    if use_ollama:
        console.print(
            f"[green]✓[/green] inference  → bundled Ollama "
            f"({settings.ollama_chat_model} + {settings.ollama_embed_model}, "
            "pulled on first `up`)"
        )
    console.print()
    console.print("[bold]KB_ROUTER_TOKEN[/bold] (save this):")
    console.print(f"  {token}")
    console.print()
    console.print(
        "Next: [cyan]agentibrain scaffold[/cyan] to seed the vault, "
        "then [cyan]agentibrain up[/cyan] to start the stack."
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


def _resolve_token(settings: BrainSettings, token: str | None) -> str:
    """Bearer token from the flag/env, else the deployment's own .env, else exit 2."""
    if token:
        return token
    env_path = settings.config_dir.expanduser() / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("KB_ROUTER_TOKEN="):
                found = line.split("=", 1)[1].strip()
                if found:
                    return found
    console.print("[red]no KB_ROUTER_TOKEN — set env var or run `agentibrain init`[/red]")
    sys.exit(2)


_STAGE_MARK = {
    "ok": "[green]✓[/green]",
    "warn": "[yellow]![/yellow]",
    "fail": "[red]✗[/red]",
}


def _render_kv(value: Any, indent: str = "    ") -> None:
    """Print a stage's evidence, one fact per line, nested dicts flattened.

    Server text is printed with markup disabled. Rich reads square brackets as
    style tags, and this content is full of them — `[nuclear]` severities, file
    paths, Python tracebacks in error_tail. Interpreted as markup they either
    vanish from the output or raise on an unknown style, losing the very
    evidence the report exists to carry.
    """
    for key, val in value.items():
        if key in {"status", "hint"}:
            continue
        if isinstance(val, dict):
            if not val:
                continue
            inner = " ".join(f"{k}={v}" for k, v in val.items())
            console.print(f"{indent}{key}: {inner}", markup=False)
        elif isinstance(val, list):
            if not val:
                continue
            console.print(f"{indent}{key}: {', '.join(str(v) for v in val[:8])}", markup=False)
        elif val is not None:
            console.print(f"{indent}{key}: {val}", markup=False)


def _local_buffers() -> tuple[int, list[str]]:
    """Count markers still sitting in the agentihooks outbox on THIS machine.

    Invisible to the server: a buffered marker has not been sent, so brain-api
    cannot know it exists. A brain that looks quiet because the writer is
    buffering is a different fault from a brain that is not being written to,
    and only the client can tell them apart.
    """
    import os as _os

    outbox = Path(
        _os.environ.get(
            "BRAIN_WRITER_OUTBOX",
            str(Path.home() / ".agentihooks" / "brain-outbox"),
        )
    ).expanduser()
    dirs = [outbox, outbox.with_name(outbox.name + "-backlog")]
    total = 0
    detail: list[str] = []
    for d in dirs:
        n = len(list(d.glob("*.json"))) if d.is_dir() else 0
        total += n
        detail.append(f"{d.name}={n}")
    return total, detail


def _get_json(
    base: str, path: str, token: str, timeout: float
) -> tuple[dict | None, str | None, int | None]:
    """GET a health endpoint.

    Returns (payload, error, http_status). A degraded endpoint that answers
    with JSON is a result, not an error. The status code comes back separately
    so the caller can tell "this build has no such endpoint" (404) from "this
    host is unreachable" — advice for one is wrong for the other.
    """
    try:
        r = httpx.get(
            f"{base}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=timeout
        )
    except httpx.HTTPError as e:
        return None, f"{type(e).__name__}: {e}", None
    # Status is checked BEFORE the body. FastAPI answers an unknown route with
    # a perfectly valid `{"detail":"Not Found"}`, so parsing first would accept
    # a 404 as a health report — the caller would then read no stages, no
    # error, and no explanation of why.
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}: {r.text[:200]}", r.status_code
    try:
        return r.json(), None, r.status_code
    except ValueError:
        return None, f"non-JSON response: {r.text[:200]}", r.status_code


@main.command("check")
@click.option("--deps-only", is_flag=True, help="Only the dependency check (/health/deep).")
@click.option("--pipeline-only", is_flag=True, help="Only the pipeline check (/health/pipeline).")
@click.option("--json", "as_json", is_flag=True, help="Emit both payloads as JSON.")
@click.option("--brain-url", envvar="BRAIN_URL", help="Override brain-api base URL.")
@click.option(
    "--token",
    envvar="KB_ROUTER_TOKEN",
    help="Bearer token (defaults to env / settings).",
)
def check_cmd(
    deps_only: bool,
    pipeline_only: bool,
    as_json: bool,
    brain_url: str | None,
    token: str | None,
) -> None:
    """Verify the brain works — dependencies AND the loop that runs on them.

    Two questions, both answered server-side so this works against a remote
    deployment as well as a local one:

    \b
      dependencies (/health/deep)  — can every dependency be reached: a real
        vault write, a real embedding with a dimension check, a real one-token
        completion through the inference gateway.
      pipeline (/health/pipeline)  — is data actually moving: markers arriving,
        the tick queue draining and succeeding, arcs ranked, lessons reconciled
        and fed back, signals broadcasting and expiring, the feed reaching a
        session, and every producer present in the index.

    Plus one check the server cannot make: markers still buffered in this
    machine's agentihooks outbox, which look like silence from the other side.

    Exit 0 clean · 1 broken · 2 degraded.
    """
    import json as _json

    if deps_only and pipeline_only:
        console.print("[red]--deps-only and --pipeline-only are mutually exclusive[/red]")
        sys.exit(2)

    settings = _load_settings()
    base = (brain_url or settings.brain_url).rstrip("/")
    token = _resolve_token(settings, token)

    run_deps = not pipeline_only
    run_pipeline = not deps_only

    deps: dict | None = None
    deps_err: str | None = None
    pipe: dict | None = None
    pipe_err: str | None = None
    pipe_status: int | None = None

    if run_deps:
        deps, deps_err, _ = _get_json(base, "/health/deep", token, 60.0)
    if run_pipeline:
        pipe, pipe_err, pipe_status = _get_json(base, "/health/pipeline", token, 30.0)

    if as_json:
        payload: dict[str, Any] = {"brain_url": base}
        if run_deps:
            payload["dependencies"] = deps if deps is not None else {"error": deps_err}
        if run_pipeline:
            payload["pipeline"] = pipe if pipe is not None else {"error": pipe_err}
        console.print_json(_json.dumps(payload))
    else:
        if run_deps:
            console.print("[bold]dependencies[/bold]")
            if deps is None:
                console.print(
                    f"  [red]✗ GET {base}/health/deep failed: {escape(str(deps_err))}[/red]"
                )
            else:
                for name, detail in (deps.get("checks") or {}).items():
                    if not isinstance(detail, dict):
                        console.print(f"  [red]✗[/red] [bold]{name}[/bold]: {escape(str(detail))}")
                        continue
                    mark = "[green]✓[/green]" if detail.get("ok") else "[red]✗[/red]"
                    console.print(f"  {mark} [bold]{name}[/bold]")
                    for key, value in detail.items():
                        if key == "ok":
                            continue
                        if key == "checks" and isinstance(value, dict):
                            for sub_name, sub in value.items():
                                if not isinstance(sub, dict):
                                    console.print(f"      {sub_name}: {sub}", markup=False)
                                    continue
                                sub_mark = "[green]✓[/green]" if sub.get("ok") else "[red]✗[/red]"
                                sub_detail = " ".join(
                                    f"{k}={v}" for k, v in sub.items() if k != "ok"
                                )
                                console.print(
                                    f"      {sub_mark} {escape(f'{sub_name}: {sub_detail}')}"
                                )
                            continue
                        console.print(f"      {key}: {value}", markup=False)

        if run_pipeline:
            console.print("\n[bold]pipeline[/bold]")
            if pipe is None:
                console.print(
                    f"  [red]✗ GET {base}/health/pipeline failed: {escape(str(pipe_err))}[/red]"
                )
                if pipe_status == 404:
                    console.print(
                        "  [yellow]this brain-api predates /health/pipeline — "
                        "`agentibrain build` to update it[/yellow]"
                    )
            else:
                for name, stage in (pipe.get("stages") or {}).items():
                    if not isinstance(stage, dict):
                        console.print(f"  [red]✗[/red] [bold]{name}[/bold]: {escape(str(stage))}")
                        continue
                    status = stage.get("status", "fail")
                    console.print(f"  {_STAGE_MARK.get(status, '?')} [bold]{name}[/bold]")
                    _render_kv(stage, indent="      ")
                    hint = stage.get("hint")
                    if hint:
                        colour = "red" if status == "fail" else "yellow"
                        console.print(f"      [{colour}]→ {escape(str(hint))}[/{colour}]")

        buffered, buf_detail = _local_buffers()
        mark = "[yellow]![/yellow]" if buffered else "[green]✓[/green]"
        console.print(f"\n{mark} [bold]local outbox[/bold] ({' '.join(buf_detail)})")
        if buffered:
            console.print(
                f"      [yellow]→ {buffered} marker(s) buffered on this machine and not yet "
                "sent — run `agentibrain sync` to replay them[/yellow]"
            )

    # Exit contract mirrors `sync`: 0 clean, 1 hard failure, 2 degraded.
    if (run_deps and deps is None) or (run_pipeline and pipe is None):
        sys.exit(1)
    states = []
    if run_deps and deps:
        # A dependency that does not work is a hard failure, not a degradation:
        # /health/deep only reports "degraded", and the pre-existing contract
        # for that was exit 1. Degraded is reserved for the pipeline, where it
        # means "flowing, but behind".
        states.append("ok" if deps.get("status") == "ok" else "broken")
    if run_pipeline and pipe:
        states.append(pipe.get("status", "unknown"))
    if "broken" in states or "unknown" in states:
        if not as_json:
            console.print("[red]status: broken[/red]")
        sys.exit(1)
    if "degraded" in states:
        if not as_json:
            console.print("[yellow]status: degraded[/yellow]")
        sys.exit(2)
    if not as_json:
        console.print("[green]all checks passed[/green]")
    sys.exit(0)


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

    token = _resolve_token(settings, token)
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

    console.print(f"  waiting (≤{TICK_WAIT_SECONDS // 60} min)…")
    import time as _time

    # A tick's AI phase may legitimately run for BRAIN_LLM_TIMEOUT_SECONDS, so
    # this can be silent for ten minutes. Silence that long is indistinguishable
    # from a hang, and an operator who cannot tell the difference kills the
    # command — so narrate: the drain's pickup, then a heartbeat with elapsed
    # time. Nothing here polls faster than before; only the reporting changed.
    deadline = _time.time() + TICK_WAIT_SECONDS
    started = _time.time()
    picked_up = False
    stall_hinted = False
    next_beat = started + _TICK_HEARTBEAT_SECONDS
    while _time.time() < deadline:
        try:
            s = httpx.get(f"{base}/tick/{job_id}", headers=headers, timeout=10.0)
            s.raise_for_status()
            status = s.json()
        except httpx.HTTPError:
            _time.sleep(2)
            continue

        state = status.get("status")
        elapsed = _time.time() - started

        if state in {"completed", "failed"}:
            console.print(f"  [bold]{state}[/bold] after {round(elapsed)}s")
            console.print(status)
            sys.exit(0 if state == "completed" else 1)

        # The record leaves requested/ the moment the drain claims it, so a
        # 404-ish "unknown" here means it is mid-run — that is the transition
        # worth announcing, because it separates "the drain is dead" from "the
        # model is thinking".
        if not picked_up and state != "pending":
            picked_up = True
            console.print(f"  [cyan]picked up by tick-drain[/cyan] after {round(elapsed)}s")
        if not stall_hinted and not picked_up and elapsed > 75:
            stall_hinted = True
            console.print(
                "  [yellow]still queued after 75s — tick-drain normally claims a job within "
                "~30s. Check `agentibrain status` and `agentibrain logs tick-drain --since "
                "5m`; an image older than the code needs `agentibrain build`.[/yellow]"
            )
        if _time.time() >= next_beat:
            next_beat = _time.time() + _TICK_HEARTBEAT_SECONDS
            console.print(
                f"  …still running ({round(elapsed / 60)}m) — the AI phase may take up to "
                "BRAIN_LLM_TIMEOUT_SECONDS. `agentibrain logs tick-cron --since 5m` shows "
                "the phase it is in."
            )
        _time.sleep(3)

    console.print(
        f"[yellow]gave up waiting after {TICK_WAIT_SECONDS // 60} min — the tick may still "
        "be running. `agentibrain logs tick-cron --since 20m` has its phases; raise "
        "AGENTIBRAIN_TICK_WAIT_SECONDS if your model is slower than that.[/yellow]"
    )
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

    token = _resolve_token(settings, token)
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

    console.print(f"  waiting (≤{TICK_WAIT_SECONDS // 60} min)…")
    import time as _time

    deadline = _time.time() + TICK_WAIT_SECONDS
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


PROFILES_ROOT = Path(__file__).parent / "profiles"


def _packaged_profile(name: str) -> Path:
    profile_dir = PROFILES_ROOT / name
    if not (profile_dir / "profile.yml").is_file():
        console.print(
            f"[red]Packaged profile '{name}' is missing:[/red] {escape(str(profile_dir))}"
        )
        console.print("Reinstall agentibrain — the wheel was built without its profile data.")
        sys.exit(1)
    return profile_dir


def _agentihooks_bin() -> str:
    # Same-environment sibling wins over PATH: a venv install must not drive a
    # stray ~/.local/bin agentihooks holding different state.
    sibling = Path(sys.executable).parent / "agentihooks"
    if sibling.is_file():
        return str(sibling)
    found = shutil.which("agentihooks")
    if found:
        return found
    console.print("[red]agentihooks not found on PATH.[/red]")
    console.print("Install it first: [bold]pip install agentihooks[/bold]")
    sys.exit(1)


@main.command("install")
@click.option(
    "--name", default="brain", show_default=True, help="Alias to register with agentihooks."
)
@click.option(
    "--profile",
    "profile_name",
    default="brain",
    show_default=True,
    help="Packaged profile to link.",
)
@click.option(
    "--for-target", default=None, help="Restrict the chain edit to one agentihooks target."
)
@click.option(
    "--no-init", is_flag=True, help="Register the link but skip the agentihooks re-install."
)
@click.option(
    "--dry-run", is_flag=True, help="Print the agentihooks command instead of running it."
)
def install_cmd(
    name: str,
    profile_name: str,
    for_target: str | None,
    no_init: bool,
    dry_run: bool,
) -> None:
    """Link the packaged brain profile into the agentihooks chain."""
    profile_dir = _packaged_profile(profile_name)
    cmd = [_agentihooks_bin(), "link-profile", "link", str(profile_dir), "--name", name]
    if for_target:
        cmd += ["--for-target", for_target]
    if no_init:
        cmd.append("--no-init")

    console.print(f"[green]→[/green] {escape(' '.join(cmd))}", soft_wrap=True)
    if dry_run:
        return

    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)


@main.command("version")
def version_cmd() -> None:
    """Print the kernel version."""
    console.print(__version__)


if __name__ == "__main__":
    main()
