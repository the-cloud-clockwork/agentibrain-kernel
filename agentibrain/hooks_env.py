"""Brain-owned client settings consumed by agentihooks.

Every brain value lives in the kernel's own environment file. Agentihooks
allowlists those values into its process but does not define or persist them.
The surrounding process environment remains the deployment-level override.
"""

from __future__ import annotations

import os
from pathlib import Path

MANAGED_ENV_NAME = "agentibrain.env"
OUTBOX_DIRS = ("brain-outbox", "brain-outbox-backlog")


def hooks_home() -> Path:
    return Path(os.environ.get("AGENTIHOOKS_HOME", str(Path.home() / ".agentihooks"))).expanduser()


def managed_env_path() -> Path:
    return hooks_home() / MANAGED_ENV_NAME


def client_defaults(vault_path: Path | None) -> dict[str, str]:
    """Brain-owned client settings consumed by agentihooks.

    agentihooks adopts these from that file, so they live beside the bearer and
    follow the vault and outbox this machine actually has. Without a local
    vault the two vault paths stay empty, and upsert_env_values skips empties.
    """
    feed = vault_path.expanduser() / "brain-feed" if vault_path is not None else None
    return {
        "BRAIN_CHANNEL": "brain",
        "BRAIN_HOT_ARCS_TOP_N": "5",
        "BRAIN_HTTP_TIMEOUT": "3",
        "BRAIN_HTTP_TOKEN": "",
        "BRAIN_PAYLOAD_MAX_BYTES": "1536",
        "BRAIN_REFRESH_INTERVAL": "30",
        "BRAIN_SOURCE_TYPE": "file",
        "BRAIN_SOURCE_PATH": str(feed) if feed else "",
        "BRAIN_ENABLED": "true",
        "AMYGDALA_ENABLED": "true",
        "AMYGDALA_SIGNAL_PATH": str(feed / "amygdala-active.md") if feed else "",
        "BRAIN_WRITER_ENABLED": "true",
        "BRAIN_WRITER_MAX_MARKERS": "5",
        "BRAIN_WRITER_OUTBOX": str(hooks_home() / OUTBOX_DIRS[0]),
    }


def ensure_outbox_dirs() -> list[Path]:
    """Create the marker buffers user-owned.

    compose binds ``~/.agentihooks`` into the tick container. Absent at first
    ``up``, the Docker daemon creates it root:root and the writer can never
    buffer a marker again.
    """
    created = []
    for name in OUTBOX_DIRS:
        d = hooks_home() / name
        d.mkdir(parents=True, exist_ok=True)
        created.append(d)
    return created


def sweep_managed_file() -> Path | None:
    """Remove the projected copy earlier versions wrote into agentihooks' chain.

    agentihooks now reads the brain's own .env, so that file is a second copy of
    the bearer whose only future is to go stale on the next rotation. Only a
    file carrying this project's header is removed; anything the operator wrote
    is left alone.
    """
    path = managed_env_path()
    if not path.is_file():
        return None
    try:
        if "Managed by `agentibrain install`" not in path.read_text(encoding="utf-8"):
            return None
        path.unlink()
    except OSError:
        return None
    return path


def _parse_brain_env() -> dict[str, str]:
    """Brain-owned settings, for when agentihooks is not importable."""
    home = Path(os.environ.get("AGENTIBRAIN_HOME", str(Path.home() / ".agentibrain")))
    resolved: dict[str, str] = {}
    files = [home / ".env"]
    for env_file in files:
        if not env_file.is_file():
            continue
        seen: set[str] = set()
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                value = value[1:-1]
            elif "#" in value:
                value = value[: value.index("#")].rstrip()
            if not key or key in seen:
                continue
            seen.add(key)
            resolved[key] = value
    return resolved


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("true", "1", "yes")


def resolve_consumer_config() -> dict:
    """What agentihooks itself would resolve on this machine.

    Imports agentihooks when it is importable, so the answer comes from the
    consumer's own resolution rather than a second implementation of it. The
    file-chain parse is the fallback for a kernel installed without it.

    ``token`` is returned so a caller can authenticate a probe with it. It is
    never rendered — callers display ``token_present``.
    """
    try:
        from hooks import config as hooks_config

        hooks_config.reload_brain_env(force=True)
        token = hooks_config.BRAIN_HTTP_TOKEN
        return {
            "source": "agentihooks",
            "brain_url": hooks_config.BRAIN_URL,
            "reader_enabled": bool(hooks_config.BRAIN_ENABLED),
            "writer_enabled": bool(hooks_config.BRAIN_WRITER_ENABLED),
            "token": token,
            "token_present": bool(token),
        }
    except Exception:  # noqa: BLE001 — agentihooks absent or mid-upgrade
        pass

    env = _parse_brain_env()

    def _get(key: str) -> str:
        return os.environ.get(key) or env.get(key, "")

    url = _get("BRAIN_URL").rstrip("/")
    token = _get("BRAIN_HTTP_TOKEN") or _get("KB_ROUTER_TOKEN")
    default = "true" if url else "false"
    return {
        "source": "env-chain",
        "brain_url": url,
        "reader_enabled": _truthy(_get("BRAIN_ENABLED") or default),
        "writer_enabled": _truthy(_get("BRAIN_WRITER_ENABLED") or default),
        "token": token,
        "token_present": bool(token),
    }
