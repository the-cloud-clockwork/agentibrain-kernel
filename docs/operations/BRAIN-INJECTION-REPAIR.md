# Brain Injection Repair — 2026-07-20

Six confirmed root causes in what the brain injects into every agent session,
found by three parallel adversarial investigations. This file is the work
order; check items off as they ship.

## Why this matters

Every Claude Code session on the fleet gets a `BROADCAST` block from the brain
at SessionStart and on every turn thereafter (persistent broadcasts bypass
delivery-dedup). Today that payload is ~930 tokens of which the largest single
block — the hot-arcs table — carries titles scraped from the first line of the
operator's first message, so agents read `hey` and `<local-command-caveat>...`
as the fleet's hottest work. Downstream, the operator-intent LLM hallucinates
narrative off those titles.

## Root causes

| # | Defect | Root cause | Tier |
|---|---|---|---|
| 1 | Phantom `[ALERT]` injected for 24h+ | WSL2 brain-feed rsync had no `--delete`; vault cleared the file, mirror kept it forever | 0 (done) |
| 2 | Hot arcs carry no meaning | **Synthesis stage designed, scaffolded, never built** — prompt lists "Unsynthesized Arcs" but has no Task 6; no `parse_synthesis`/`apply_synthesis`; arc rows carry no body to summarize from | 2 |
| 3 | Titles are junk | `cluster.py` title = first 60 chars of first line of first user message, unfiltered. Claude Code's `<local-command-caveat>` boilerplate is transcript-encoded as a `user` turn | 1 |
| 4 | Agents cannot drill into an arc | `write_hot_arcs_md` renders `[[cluster_id]]`; `brain_get_arc` takes a bare string → brackets 404. Framing says "no action required" | 1 |
| 5 | `'\''` corruption in vault content | `brain_outbox_sync.sh` escapes for a single-quote context, then interpolates into a **double-quoted** printf. 39 files, 2026-04-11 → 2026-07-20, still active | 1 + 4 |
| 6 | April `@inject` immortal | `all_injects.extend()` unconditional — no age/status/heat filter, only hash-dedup. Source arc heat 0, 102d old, `status: active` because graduation never writes `status: graduated` | 1 + 4 |

Bonus bugs found in passing:
- **Heat dead zone** — promote fires `heat >= 3`, demote `heat < 2`; an arc at
  exactly 2 satisfies neither and is stranded in `frontal-lobe/conscious/`.
- **Severity quantization** — `amygdala.py` collapses 3 severities into 2, so a
  `warning` is written as `severity: alert` and renders `[ALERT]`.
- **Amygdala clear race** — clear requires `elapsed >= CLEAR_WINDOW_SEC` (900s)
  while the Redis `last_event_ts` key still exists, but that key has TTL 1800s.
  Miss the 900–1800s window (consumer restart) and the file strands forever.

## Decisions taken

- **Do not inject `frontal-lobe/conscious/`.** It is write-only accumulation
  (90 files, April→now, no pruning, 20 with no heat field). `hot-arcs.md` is
  already that view, freshly computed per tick and `is_arc()`-filtered.
- **Summaries persist to arc frontmatter, not regenerated per tick.** Phase 1
  (`brain_keeper`) is deliberately LLM-free so the feed still works when
  `INFERENCE_URL` is down. Summaries are produced in Phase 3/4 and written to
  `summary:`, surfacing on the next tick. Bounded by arc creation rate, not
  tick cadence.
- **The query call-to-action belongs to the producer** (`brain_keeper` writes
  it into `hot-arcs.md`), because only the brain knows its own MCP tool names.
  agentihooks keeps a neutral provenance marker — generic, must hold for every
  channel, must not depend on producers behaving well.
- **Budget is a redistribution, not an expansion.** ~930 tokens injected today
  against a ~1200 ceiling. Summaries are paid for by cutting intent's 951 bytes
  of speculation.

---

## Tier 0 — stop the bleeding (no code)

- [x] `--delete` on the WSL2 brain-feed rsync cron; stale `amygdala-active.md`
      removed from the local mirror.

## Tier 1 — mechanical fixes

**agentihooks**
- [x] A. `scripts/brain_outbox_sync.sh` — drop the bogus shell escaping
      (`.replace("'", "'\\''")` + backslash doubling). Content is consumed via
      a double-quoted `printf`, so no escaping is correct. Stops new corruption.
- [x] B. `hooks/context/brain_adapter.py` — `_wrap_with_framing` must mark
      provenance without commanding inaction. "no action required" → a neutral
      marker.

**agentibrain-kernel**
- [x] C. `brain_keeper.py::write_hot_arcs_md` — drop `[[ ]]` wrapper, render
      `summary:` when present, append the MCP query trailer.
- [x] D. `cluster.py` — reject Claude Code boilerplate / bare paths / system
      strings as titles; fall back `first_user_prompt` → `last_user_prompt` →
      `project + date`.
- [x] E. `brain_keeper.py` — close the heat dead zone (demote on
      `heat < PROMOTE`, not `heat < DEMOTE`).
- [x] F. `amygdala.py` — stop quantizing `warning` into `severity: alert`; make
      the clear path robust to a missing Redis key (fall back to file mtime).
- [x] G. `brain_keeper.py` — filter `@inject` collection by arc status/age, and
      make graduation actually write `status: graduated`.

## Tier 2 — arc synthesis (the real fix)

- [x] H. `brain_tick_prompt.py` — carry `project`, `created`, ignition excerpt
      and marker content on unsynthesized arc rows; add **Task 6: synthesize a
      one-sentence summary** for each.
- [x] I. `brain_apply.py` — `parse_summaries()` + `apply_summaries()`: write
      `summary:` into arc frontmatter and flip `synthesized: false → true`.

## Tier 3 — intent quality

- [x] J. `brain_tick_prompt.py` — feed the intent section `project`, arc age,
      `@decision`/`@milestone` content, and the **previous tick's intent** (there
      is zero tick-to-tick continuity today). Instruct it to cite arc ids rather
      than narrate.

## Tier 4 — vault data remediation

- [x] K. Remove the stale `@inject` block from its 5 source files.
- [x] L. Repair the 39 `'\''`-corrupted files. Per-file review required — some
      are technical arcs that may legitimately quote shell escaping, so a blind
      global substitution risks false positives.

---

## Outcome — 2026-07-20

All tiers shipped.

| Repo | Commit | Carries |
|---|---|---|
| agentihooks | `a77b4b7` | A, B |
| agentibrain-kernel | `cbbc055` | C, D, E, F, G, H, I, J |

Vault remediation (Tier 4), applied directly to vault content:
- The stale `@inject` marker in `left/projects/agentihooks-bundle.md` (both
  occurrences) renamed to `inject-retired`, text preserved as history. The other
  four sources are all `created: 2026-04-09` and now age out automatically via
  `BRAIN_STALE_INJECT_DAYS=30`.
- 39 files / 111 occurrences of `'\''` repaired. Reviewed for false positives
  first — every occurrence was a prose apostrophe (`caller's`, `I'll`), none was
  quoted shell code. Backup: `vault-backups/escape-fix-20260720.tar.gz`.

### Verified

- `apply_summaries` against a real vault arc: `summary:` written,
  `synthesized: true` flipped, re-run is a no-op.
- Round-trip through `markers.extract_all` into `write_hot_arcs_md` — the arc
  formerly titled `hey` renders its summary, a bare copy-pasteable cluster_id,
  and the MCP query trailer.
- `parse_summaries` against a realistic model response: duplicate ids dropped,
  `SKIP` honoured, non-matching lines ignored, YAML-hostile chars sanitised.
- Title deriver against all four live garbage cases.
- Outbox escaping fixed end-to-end: an apostrophe-heavy marker lands clean.

### Follow-ups raised, not actioned

- A tick AI-output file under `brain-feed/ticks/` was found to contain a
  short-lived credential in plaintext (since expired). The value is unimportant;
  the pattern is: tick AI output echoes whatever it was fed, so a credential
  appearing in a signal is persisted to the vault and re-injected on every
  subsequent session. This is what `redact.py` was built to close.
- Existing arcs keep their scraped titles; the title fix only affects arcs
  created from here on. Existing arcs get meaning from `summary:` as the tick
  works through the backlog at 25/tick.
- `apply_merges` still concatenates arc bodies with no marker-level dedup, so
  historical duplicate markers survive merges.

### Live verification, 2026-07-20 19:09 → 19:40 UTC

First tick on the synthesis build generated 23 good summaries and applied
**zero** — the model wrapped each emitted line in backticks, faithfully copying
the prompt's own format example (every other section writes its examples that
way too). The regex only tolerated a leading `-`/`*`. Fixed in `cba9f9e`; the
missing summary count in the "Parsed AI output" line is why this read as "no
summaries produced" rather than "none parsed", so that count was added too.

Second tick: `23 summaries` parsed and applied, health 5/10 → 6/10.
Third tick (Phase 1 only) re-rendered the feed from the new frontmatter.

Before → after, same arc:

| Arc | Was | Now |
|---|---|---|
| `<date>-hablar-sobre-cron…` | Hablar sobre cron y seguimiento automático hoy | Scheduled pipeline trigger activated; a threshold clarified as applying to the active subset rather than the whole pool |
| `<date>-hey-ca22a3f9b394` | hey | Short session verifying brain and tooling responsiveness; no substantive work output |
| `<date>-local-command-caveat…` | \<local-command-caveat\>Caveat: The messages below were genera | Local command session; **a bearer token was inadvertently exposed in this transcript and requires rotation** |

The third row is the point: an arc that was indistinguishable boilerplate now
surfaces a credential exposure. Note also that the model declined to invent
substance for thin arcs ("no substantive work output", "no actionable decisions
recorded") rather than hallucinating, which is what the tightened prompt asked
for.

`inject.md` now reads "No inject blocks." — the April block is gone.
