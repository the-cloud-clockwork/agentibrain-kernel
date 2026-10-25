# Project scope migration

How a brain moves from one fleet wide memory to memory scoped by canonical
project, and how to step back. Tracks issue 89; the acceptance suite is
`tests/integration/project_scope/`.

## Project identity

A project is its canonical origin remote, `host/owner/repo`, lower case on
GitHub. The agentihooks client and the kernel derive it the same way:

| Source | Resolution |
|---|---|
| Claude session | Its recorded working folder, through `git rev-parse --git-common-dir` and the `origin` remote. A linked worktree resolves to its repository and keeps its worktree folder as provenance. |
| Codex session | The `repository_url` Codex records in the rollout's session metadata, else its working folder as above. |
| Marker | The `project_id` the client sends, checked against the token's project grant. |

A session whose folder no longer exists on the extraction host, or whose
repository has no `origin`, stays unattributed. The kernel never guesses a
project from a folder name.

## Path from source to delivery

1. `extract.py` reads Claude transcripts (`--projects-dir`, `EXTRACT_PROJECTS_DIR`)
   and Codex rollouts (`--codex-dir`, `EXTRACT_CODEX_DIR`, skipped when absent).
   Compose mounts `CODEX_SESSIONS_HOST` (default `~/.codex/sessions`) read only,
   the same sanctioned transcript exception to the no shared filesystem rule
   as the Claude projects mount, until the session source of SV2-ING-01
   replaces both.
2. `cluster.py` groups sessions by canonical project and task and writes the
   project on each arc.
3. `POST /marker` stores project provenance on lessons, decisions, milestones
   and signals; the outbox drain replays them with the same idempotency key.
   A signal posted with `scope=fleet` is written with `scope: fleet` and served
   to every project as a fleet warning.
4. The tick writes one snapshot per project, fleet warnings and one inferred
   intent per active project. `embed_arcs.py` indexes arcs with their
   `projects` and every lesson and decision as a scoped knowledge item.
5. Readers: `GET /feed?project=`, embeddings `POST /search` with `project_id`,
   and the MCP tools `brain_search_arcs` and `kb_search` with `project`. A
   project scoped `kb_search` skips the vault text search, because a text
   mention of a repository is not a project match.

## Rolling out

- Deploy the kernel, then let one tick run. New arcs and markers carry scope
  from then on.
- Only arcs whose `project` is a canonical id are indexed with `projects`;
  legacy arcs grouped by folder basename stay unattributed in the index.
- Arcs embedded before this release have no `projects` in their index rows.
  Raise `EMBEDDING_STATE_GENERATION` once to re-embed every arc; until then
  project scoped arc searches return only arcs indexed after the release.
- Compose installs that run Codex: confirm `~/.codex/sessions` exists on the
  host before `docker compose up -d --build`, or set `CODEX_SESSIONS_HOST`.
- Claude extraction inside a container sees transcript folders but not the
  checkouts they name, so container extracted Claude arcs stay unattributed
  until the session source of SV2-ING-01 supplies event time project metadata.
  Codex arcs carry their recorded remote and are attributed in the container.

## Rolling back

- Deploy the previous kernel image. Stored scope is additive: lesson and
  decision provenance, arc `project` frontmatter, `scope: fleet` on signal
  files and `projects` in index metadata stay in place and are ignored by the
  older code.
- Older clients keep calling the unscoped `GET /feed`, which is unchanged.
- To turn scoped retrieval back on, redeploy this release; no backfill is
  needed because nothing was deleted.
- Do not delete `brain-feed/projects/`, `brain-feed/project-intent/` or the
  marker registry as if they were caches: the marker registry is the
  authoritative record of marker identity, and the next tick rewrites the
  snapshots only from what remains in the vault.
