"""``brain`` CLI entry point.

Phase 1 stubs. Implementations land in Phase 5 (bootstrap + scaffold).
"""

from __future__ import annotations

import sys

import click
from rich.console import Console

from agentibrain import __version__

console = Console()


@click.group()
@click.version_option(__version__, prog_name="brain")
def main() -> None:
    """agentibrain — standalone brain + KB kernel."""


@main.command()
@click.option("--vault", type=click.Path(), help="Path to the vault.")
@click.option("--local", "local_mode", is_flag=True, help="Use MinIO instead of S3.")
@click.option("--s3-bucket", help="S3 bucket name (required without --local).")
@click.option("--s3-endpoint", help="S3 endpoint override (e.g. for MinIO).")
@click.option("--postgres-url", help="External Postgres DSN. Defaults to bundled.")
@click.option("--redis-url", help="External Redis URL. Defaults to bundled.")
@click.option("--openai-key", help="OpenAI API key.", envvar="OPENAI_API_KEY")
def init(**kwargs: object) -> None:
    """Initialize a new brain deployment (writes config + prepares compose)."""
    console.print("[yellow]not implemented (Phase 5)[/yellow]")
    sys.exit(2)


@main.command("up")
def up_cmd() -> None:
    """Start the brain stack (docker compose up -d + migrations)."""
    console.print("[yellow]not implemented (Phase 5)[/yellow]")
    sys.exit(2)


@main.command("down")
def down_cmd() -> None:
    """Stop the brain stack (docker compose down)."""
    console.print("[yellow]not implemented (Phase 5)[/yellow]")
    sys.exit(2)


@main.command("status")
def status_cmd() -> None:
    """Show health of all services."""
    console.print("[yellow]not implemented (Phase 5)[/yellow]")
    sys.exit(2)


@main.command("tick")
def tick_cmd() -> None:
    """Trigger a manual cognitive tick."""
    console.print("[yellow]not implemented (Phase 5)[/yellow]")
    sys.exit(2)


@main.command("scaffold")
@click.argument("vault_path", type=click.Path(), required=False)
@click.option("--force-upgrade", is_flag=True, help="Overwrite existing .brain-schema.")
def scaffold_cmd(vault_path: str | None, force_upgrade: bool) -> None:
    """Seed the vault folder layout."""
    console.print("[yellow]not implemented (Phase 5)[/yellow]")
    sys.exit(2)


@main.command("version")
def version_cmd() -> None:
    """Print the kernel version."""
    console.print(__version__)


if __name__ == "__main__":
    main()
