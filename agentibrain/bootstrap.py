"""Render compose, write config, run migrations — implemented in Phase 5."""

from __future__ import annotations

from agentibrain.config import BrainSettings


def render_compose(settings: BrainSettings) -> str:
    """Render the docker-compose file for the configured mode. Phase 5."""
    raise NotImplementedError("bootstrap.render_compose lands in Phase 5")


def run_migrations(settings: BrainSettings) -> None:
    """Apply SQL migrations under ``migrations/`` to Postgres. Phase 5."""
    raise NotImplementedError("bootstrap.run_migrations lands in Phase 5")


def compose_up(settings: BrainSettings) -> None:
    """Invoke ``docker compose up -d`` on the rendered file. Phase 5."""
    raise NotImplementedError("bootstrap.compose_up lands in Phase 5")


def compose_down(settings: BrainSettings) -> None:
    """Invoke ``docker compose down``. Phase 5."""
    raise NotImplementedError("bootstrap.compose_down lands in Phase 5")
