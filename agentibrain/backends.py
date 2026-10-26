"""The external state a brain owns, and refusal when another brain already owns it.

Separate config roots, compose projects or Kubernetes namespaces prove nothing
when two brains still reach one vault, one database principal or schema, or
one Redis keyspace. A binding names exactly what a brain reaches; two bindings
of different brains must share none of it. The vault itself carries an owner
claim so a misbinding is refused even when the other brain's config is not
visible here.
"""

from __future__ import annotations

import json
import tarfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from agentibrain.config import DEFAULT_BRAIN_ID, BrainSettings, config_dir, root_brain_id

VAULT_CLAIM = ".brain-instance"
CLAIM_SCHEMA = "brain-claim-v1"
FLEET_STREAMS = ("events:health", "events:host", "events:deploy", "events:system")
# Compose service names resolve only inside one stack's own network.
BUNDLED_HOSTS = frozenset({"postgres", "redis"})

brain_backend_scope_conflicts: Counter[str] = Counter()


class BackendScopeError(RuntimeError):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind


def _refuse(kind: str, detail: str) -> BackendScopeError:
    brain_backend_scope_conflicts[kind] += 1
    return BackendScopeError(kind, detail)


def namespaced(brain_id: str, key: str) -> str:
    """The default brain keeps legacy keys; a named brain prefixes every key it owns."""
    return key if brain_id == DEFAULT_BRAIN_ID else f"brain:{brain_id}:{key}"


@dataclass(frozen=True)
class Database:
    server: str
    database: str
    principal: str
    schema: str


def _database(url: str) -> Database | None:
    parts = urlsplit(url)
    if not url or parts.hostname in BUNDLED_HOSTS:
        return None
    options = unquote(parse_qs(parts.query).get("options", [""])[0])
    schema = "public"
    for option in options.split():
        name, _, value = option.removeprefix("-c").partition("=")
        if name == "search_path" and value:
            schema = value.split(",")[0].strip()
    return Database(
        server=f"{parts.hostname}:{parts.port or 5432}",
        database=parts.path.lstrip("/") or parts.username or "",
        principal=unquote(parts.username or ""),
        schema=schema,
    )


def _redis(url: str, db: str) -> str | None:
    parts = urlsplit(url)
    if not url or parts.hostname in BUNDLED_HOSTS:
        return None
    return f"{parts.hostname}:{parts.port or 6379}/{db}"


@dataclass(frozen=True)
class BackendBinding:
    brain_id: str
    vault: Path
    database: Database | None
    redis: str | None
    redis_keys: frozenset[str]

    @classmethod
    def from_settings(cls, settings: BrainSettings, env: Mapping[str, str]) -> BackendBinding:
        brain = settings.brain_id
        own = namespaced(brain, "events:brain")
        stream = env.get("EVENT_BUS_STREAM") or own
        group = namespaced(brain, "amygdala")
        read = [s for s in env.get("AMYGDALA_STREAMS", "").split(",") if s.strip()]
        read = [s.strip() for s in read] or [*FLEET_STREAMS, stream]
        keys = {stream, namespaced(brain, "amygdala:last_event_ts")}
        keys |= {f"{s}#{group}" for s in read}
        vault = Path(env.get("VAULT_ROOT_HOST") or settings.vault_path).expanduser().resolve()
        return cls(
            brain_id=brain,
            vault=vault,
            database=_database(env.get("POSTGRES_URL") or settings.postgres_url or ""),
            redis=_redis(
                env.get("REDIS_URL") or settings.redis_url or "", env.get("EVENT_BUS_DB") or "11"
            ),
            redis_keys=frozenset(keys),
        )


def scope_conflicts(a: BackendBinding, b: BackendBinding) -> list[tuple[str, str]]:
    """``(kind, detail)`` for every piece of external state two different brains share."""
    if a.brain_id == b.brain_id:
        return []
    found: list[tuple[str, str]] = []
    if a.vault == b.vault or a.vault in b.vault.parents or b.vault in a.vault.parents:
        found.append(("vault", f"vaults {a.vault} and {b.vault} overlap"))
    if a.database and b.database and a.database.server == b.database.server:
        if a.database.principal == b.database.principal:
            found.append(
                ("principal", f"both connect to {a.database.server} as {a.database.principal}")
            )
        elif (a.database.database, a.database.schema) == (b.database.database, b.database.schema):
            found.append(
                (
                    "schema",
                    f"both use {a.database.database}.{a.database.schema} on {a.database.server}",
                )
            )
    if a.redis and a.redis == b.redis and a.redis_keys & b.redis_keys:
        shared = ", ".join(sorted(a.redis_keys & b.redis_keys))
        found.append(("redis", f"both use {shared} on {a.redis}"))
    return found


def _env_values(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_path.is_file():
        return values
    for line in env_path.read_text().splitlines():
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and not key.startswith("#"):
            values[key.strip()] = value.strip().strip("'\"")
    return values


def _root_binding(root: Path, brain_id: str) -> BackendBinding:
    import yaml

    cfg = root / "config.yaml"
    raw = (yaml.safe_load(cfg.read_text()) or {}) if cfg.is_file() else {}
    payload = {k: v for k, v in raw.items() if v is not None}
    payload.update(config_dir=root, brain_id=brain_id)
    env = _env_values(root / ".env")
    return BackendBinding.from_settings(BrainSettings(**payload, _env_file=None), env)


def installed_roots() -> list[Path]:
    home = Path.home()
    roots = {config_dir(), *home.glob(".agentibrain"), *home.glob(".agentibrain-*")}
    return sorted(r for r in roots if r.is_dir())


def installed_conflicts(settings: BrainSettings) -> list[tuple[str, str, str]]:
    """``(other brain, kind, detail)`` against every other brain installed on this host."""
    own_root = settings.config_dir.expanduser().resolve()
    own = BackendBinding.from_settings(settings, _env_values(own_root / ".env"))
    found = []
    for root in installed_roots():
        other = root_brain_id(root)
        if other is None or other == settings.brain_id or root.resolve() == own_root:
            continue
        for kind, detail in scope_conflicts(own, _root_binding(root, other)):
            found.append((other, kind, detail))
    return found


def _claimed(path: Path) -> str | None:
    claim = path / VAULT_CLAIM
    if not claim.is_file():
        return None
    return json.loads(claim.read_text()).get("brain_id")


def claim_vault(root: Path, brain_id: str) -> None:
    """Claim an unclaimed vault for this brain; refuse one inside, or held by, another brain."""
    root = root.expanduser().resolve()
    for parent in root.parents:
        held = _claimed(parent)
        if held is not None and held != brain_id:
            raise _refuse("vault", f"vault {root} lies inside brain {held}'s vault {parent}")
    held = _claimed(root)
    if held == brain_id:
        return
    if held is not None:
        raise _refuse("vault", f"vault {root} belongs to brain {held}, not {brain_id}")
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f"{VAULT_CLAIM}.tmp"
    tmp.write_text(json.dumps({"schema": CLAIM_SCHEMA, "brain_id": brain_id}) + "\n")
    tmp.replace(root / VAULT_CLAIM)


def backup_vault(vault: Path, archive: Path, brain_id: str) -> Path:
    """Archive a vault this brain owns; the claim travels inside as the backup's identity."""
    claim_vault(vault, brain_id)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(vault, arcname=".")
    return archive


def restore_vault(archive: Path, vault: Path, brain_id: str) -> None:
    """Restore only a backup this brain made, only into a vault this brain owns."""
    with tarfile.open(archive, "r:gz") as tar:
        names = {m.name.removeprefix("./"): m for m in tar.getmembers()}
        member = names.get(VAULT_CLAIM)
        source = tar.extractfile(member) if member is not None else None
        if source is None:
            raise _refuse("backup", f"backup {archive} carries no brain identity")
        held = json.loads(source.read()).get("brain_id")
        if held != brain_id:
            raise _refuse("backup", f"backup {archive} belongs to brain {held}, not {brain_id}")
        claim_vault(vault, brain_id)
        tar.extractall(vault.expanduser().resolve(), filter="data")
