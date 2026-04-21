# Architecture

Canonical design docs for the brain nervous system. These files were first
authored in `antoncore/docs/brain/` and have been promoted here as the
source-of-truth for the kernel. The antoncore copies now carry a deprecation
header pointing back at this directory.

| File | Scope |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Overall system diagram, components, data flow. |
| [`READERS-GUIDE.md`](READERS-GUIDE.md) | How agents consume brain-feed + signals at SessionStart. |
| [`KEEPER.md`](KEEPER.md) | brain-keeper agent responsibilities + CLI commands. |
| [`CLUSTERS.md`](CLUSTERS.md) | Arc clustering algorithm + heat scoring. |
| [`MARKERS.md`](MARKERS.md) | `@lesson` / `@milestone` / `@signal` / `@decision` emission rules. |
| [`SYMBIOSIS.md`](SYMBIOSIS.md) | How the brain, amygdala, and fleet hooks cooperate. |
| [`TELEMETRY.md`](TELEMETRY.md) | Metrics, logs, dashboards. |
| [`MATURITY.md`](MATURITY.md) | Maturity assessment scorecard (updated per sprint). |

See also:

- [`../API.md`](../API.md) — summary of the HTTP contract (authoritative:
  [`api/openapi.yaml`](../../api/openapi.yaml)).
- [`../VAULT-SCHEMA.md`](../VAULT-SCHEMA.md) — vault folder layout owned by
  `brain scaffold`.
