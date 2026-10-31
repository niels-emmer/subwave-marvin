# Architecture

How `subwave-marvin` is put together, and why it is shaped this way. This is the durable description:
if you are changing the tool, these are the facts a change has to survive. What is *left to do* lives in
[PLAN.md](PLAN.md); the operating rules an agent follows live in [AGENTS.md](../AGENTS.md); the reasoning
behind each choice, with the alternatives that were rejected, is in [decision-log.md](decision-log.md).

## Where it sits

Three parties, and the toolkit is the only one that is neither the station nor the agent.

```
  agent (Claude Code, Codex, OpenCode, anything that reads AGENTS.md)
     │  reads AGENTS.md + skills/<name>/SKILL.md, runs one command at a time
     ▼
  scripts/subwave.py ───── admin HTTP (Basic) ──────►  controller  ──►  Icecast
     │                                                 (upstream image)  Liquidsoap
     │                                                 └──► Navidrome (library)
     │  ───── MCP /api/mcp (JSON-RPC) ──────────────►  what is on air, now
     │  ───── ssh + docker exec ───────────────────►  host: the state dir
     ▼
  instance/  (gitignored working copy: the station as files)
     │
     └── git ── history, review, undo
```

The station is a separate project. This repository ships no station code, no container image and no
binary assets; it ships a single Python file, a set of committed procedures, and a fictional demo station.

## Two constraints that shaped everything

**C1 — the repo is public.** Every instance-specific fact and all PII live in gitignored files, with
anonymised equivalents committed. That covers station identity, hostnames, credentials, persona and show
definitions, and any themed character content. This repo's own deployment is therefore deliberately
undescribed here: it is the first user of the toolkit, not its subject. Station-specific choices — which
persona hosts which show — live in `instance/character/` and are reviewed there.

**C2 — agent-agnostic.** The unit of installation is the clone, and the only universally-read artifact in
it is `AGENTS.md`. No agent-specific assumption may enter the tool, the skills or the docs. See
[skills/README.md](../skills/README.md) for why the procedure directory needs that hub.

## The finding: the documented API is not the config API

Verified against a live station. Every structural decision below follows from this table.

| Endpoint | Actually returns |
| --- | --- |
| `GET /api/personas` | 4 fields per persona (`id, name, tagline, avatar`) — even with admin auth |
| `GET /api/schedule` | the grid, plus shows cut to **7 of 26** fields |
| `GET /api/settings` (admin) | a **19-key UI summary**, not the 52-key `settings.json` |
| `GET /api/connect/openapi.json` | curated subset, auth required, **no config writes** |
| `GET /api/backup/export` | ~95 MB zip, **no `schedule.json`**, no `blocklist.json` |
| `GET /shows` | **does not exist** |

Full show definitions are unreadable over HTTP at all, so no HTTP-only path can round-trip configuration.
The repo has to be the source of truth; the station is the thing it is reconciled against.

## Three surfaces, deliberately not mixed

| Surface | Used for | Authoritative for |
| --- | --- | --- |
| State files over `ssh` + `docker exec` | reading the station as it is | `settings.json`, `schedule.json` — the whole config document |
| Admin HTTP API (`POST /api/settings`, `POST /api/shows`, `PUT /api/schedule`) | writing | nothing; it accepts and may silently ignore |
| MCP at `/api/mcp` | what is happening *now* | nothing persisted |

MCP exposes 21 tools — reads, listener requests, on-air control — and **zero config writers**. Config
never goes through MCP and on-air state never goes through `apply`. That separation is what makes the
declarative pipeline auditable: everything it touches is in git, and nothing it touches is gone in a minute.

Writing through the admin API has one hard consequence: `POST /api/settings` is a **shallow partial
merge**, so a missing field is a deleted field. A payload must therefore always be a whole object built
from the repo, never a read-modify-write of a trimmed read.

## Two capability tiers

| | Tier 1: HTTP only | Tier 2: host access |
| --- | --- | --- |
| Needs | `SUBWAVE_BASE_URL` + admin credentials | + `SUBWAVE_HOST`, `SUBWAVE_STATE_DIR`, ssh, `docker exec` |
| Create / update | author from scratch only — a lossy capture cannot be applied | yes |
| Read personas | trimmed (4 fields) | full |
| Read shows | trimmed (7 of 26) | full |
| Read the grid | yes | yes |
| Capture for editing | **lossy — cannot be applied** | yes |
| Backup | refused | complete: config + assets + `schedule.json` |

Two rules the tool must honour everywhere:

1. **Announce the tier.** Every command says which tier it is at and what that costs. Never degrade
   silently. At tier 1, `status` also probes the host it is given and prints the two `.env` lines that
   reach tier 2, because those two facts are discoverable and should not be interrogated out of the
   operator (D13).
2. **Never read-modify-write at tier 1.** Whole objects from the repo, or nothing.

## The data model

### Three documents on the station

The station's real configuration is three files in the state dir, usually root-owned and reachable only
inside the controller container:

- `settings.json` — the whole station config, **including `personas[]`** and `activePersonaId`.
- `schedule.json` — `{shows[], schedule, override}`; `schedule` maps day `"0".."6"` to 24 show ids.
- `blocklist.json` — bans and rules. The station's own export omits it, so a stock restore loses every ban.

Runtime and derived files in the same directory (`library.db*`, `secrets.env`, `icecast*`,
`liquidsoap_*.txt`, archives, logs) are never read into the repo and never hand-edited.

### The repo projection: the character layer

A **character** is the themed bundle that makes a station itself, and it is pluggable: swap the directory
and you have a different station on the same software. The split is a repo convenience — `pull` splits the
two server documents out, `apply` reassembles them.

| File | Holds | Server field it maps to |
| --- | --- | --- |
| `character/character.json` | `name`, `description`, `locale`, `timezone`, `theme`, `houseRules` | `station`, `stationDescription`, `locale`, `timezone`, `theme.active`, `djHouseRules` |
| `character/personas/*.json` | one persona per file (18 fields; `soul` *is* the system prompt) | `settings.personas[]` |
| `character/shows/*.json` | one show per file (26 fields) | `schedule.shows[]` |
| `character/schedule.json` | `{schedule, override}` — the 7×24 grid, by slug | `schedule.schedule`, `schedule.override` |

The identity fields are the exception: they are **removed** from `settings.json` and live only in
`character.json`. Keeping both copies is what once made editing the character layer a silent no-op —
`apply` sent the stale one. `houseRules` is a list here and one newline-joined string on the station.

### `ids.lock.json`

Server ids are generated (`p_xxxxxx`, `s_xxxxxx`), so the lockfile holds the slug → id map, the `capture`
record `apply` checks, and the hash of the last applied payload. It is generated, never hand-edited, and
it is what stops a rename from creating a duplicate entity. A scaffolded entity carries a local
placeholder id (`p_local-…`, `s_local-…`); a show's is stripped on the wire so the station assigns a real
one, which is written back to the file and the lock.

The `capture` record is provenance, not decoration: `tier` says how the files were obtained (`null` when
authored locally), `source` says by which path (`pull` or `authored`), and `lossy` says the capture
cannot be applied. Only `lossy` gates a write; the rest exists so the tool can explain itself.

### The redaction sentinel

`pull` replaces every secret path with `"__REDACTED__"`. That sentinel is legal in `instance/` and never
legal in an apply payload: a payload still holding it means a secret was not rehydrated, and `apply`
refuses rather than write the literal string over a live credential. At write time secrets come from the
environment (`SUBWAVE_*`, and `ANTHROPIC_API_KEY` for a redacted `llm.apiKey`).

## The write pipeline

```
pull ──► instance/ ──edit──► validate ──► diff ──► apply (dry run) ──► apply --yes ──► read-back
 live                                                                     │
                                                                     backup first
```

`apply` is ordered, and the order is load-bearing:

1. **personas + `activePersonaId`** (`POST /api/settings`) — nothing can reference a persona that does not exist.
2. **settings + character** (`POST /api/settings`) — whole top-level sections, identity merged back from `character.json`.
3. **shows** (`POST /api/shows`, upsert by `show.id`, sorted by slug) — a new show's placeholder id is
   omitted so the station assigns one, which is persisted back.
4. **grid last** (`PUT /api/schedule`) — always all 7×24 cells, because the route replaces the whole grid,
   and it is refused if any cell still names an unresolvable show.
5. **blocklist** entries and rules — after the grid; a 409 "already blocked" counts as success.
6. **station skills** — create via `POST /api/dj/skills`, update via `PUT /dj/skills/:slug/file`. Never
   deleted, and a skill the station reports as `seeded` is skipped: the image's built-ins are not the
   operator's.
7. **read-back** — `apply` re-reads the station and fails loudly if a field did not land. HTTP 200 means
   accepted, not stored, and a silent no-op is the worst failure this tool has.
8. **prune**, only under `--prune` and only after all of the above — see [AGENTS.md](../AGENTS.md).

Guards that stop the pipeline before any write: a lossy capture, a payload still holding the sentinel,
`validate` errors, mid-show without `--force-now`, tier 1 without `--no-backup`, and (for prune) a
dependency or hard refusal. `apply` also digests the payload and does nothing when the station already
matches the last apply.

**Backup and restore** are the other half of each other. `backup` copies the config documents plus the
assets (`skills/`, `sfx/`, `jingles/`, `beds/`, `themes/`, `persona-avatars/` and their manifests), skips
`library.db` unless asked, and writes a `RESTORE.md` naming where each file goes back and in what order.
`restore` refuses a backup that is incomplete or whose grid does not resolve, restores state before
assets, and rolls back to the previous state if what it wrote does not validate.

**`onair` is outside all of this.** It is the one imperative surface, and it is ephemeral by construction:
never captured, never diffed, never applied, never in git.

## Validation

Validation runs *before* any write, because a typo'd field is silently dropped by the shallow merge rather
than rejected — it is the only thing between a typo and a live config change.

- Field specs live in `scripts/subwave.py` (`PERSONA_FIELDS`, `SHOW_FIELDS`, `PERSONA_ENUMS`,
  `SHOW_ENUMS`, `PERSONA_TYPES`, `SHOW_TYPES`), not in a separate schema module.
- Enums are copied from upstream (`controller/src/schemas/persona.ts` and `show.ts`) in the product's
  ladder order, because the station bumps a persona "one rung" with `indexOf`. `moods` is deliberately
  *not* an enum: the station validates against its own installed mood vocabulary, which is read at
  validate time (`station_mood_names`).
- **Missing fields are errors; unknown fields are warnings.** A newer server may know a field this tool
  does not model; a field the tool does not model that the server *does* have would be deleted on merge.
- The grid must have exactly 7 day keys and 24 cells each, every cell resolving to a known show slug.
- Cross-references are checked: `show.personaId` and `guestPersonaIds` exist, `activePersonaId` exists.

`validate` is permissive about *values* a newer server may know. `character edit-persona` / `edit-show`
are strict, because there the value is about to be written by hand — they refuse an unknown field, a wrong
type or an enum outside the set, and restore the file if the result would fail `validate`.

## Invariants

A change must not break any of these. Each one has a failure behind it.

1. **Whole-object writes only.** Never send a read-modify-write payload.
2. **The repo is the source of truth.** No daemon, no auto-apply, no post-commit write.
3. **Verify after every write.** Read back; report, do not assume.
4. **A lossy capture is refused, not warned about.** Tier 1 cannot see 19 show fields.
5. **No deletes outside `--prune`**, and prune never runs before the writes and the read-back.
6. **`instance/` and `.env` never enter git**, and `examples/demo/` is the only station in the repo.
7. **MCP stays control-only.** No config write path through it.
8. **`character.json` is the only home for station identity.** No duplicate in `settings.json`.
9. **Announce the tier, and never degrade silently.**
10. **`tool.mjs` is left to the station's human trust review.** Installing or trusting it would bypass a
    boundary the station deliberately built.

## Repo layout

```
AGENTS.md                 agent-agnostic operating rules; the hub every agent reads
README.md                 for the person who runs the station
CONTRIBUTING.md           setup, checks, conventions
SECURITY.md               threat model and accepted risks
scripts/subwave.py        the tool: one file, standard library only
scripts/check-secrets.sh  the commit gate and CI check
skills/<name>/SKILL.md    agent procedures (discovered through AGENTS.md)
examples/demo/            committed fictional station, same shape as instance/
tests/                    offline suite; mock_station.py serves a real-shaped controller
docs/ARCHITECTURE.md      this file
docs/PLAN.md              what is left to do
docs/decision-log.md      why each choice was made, and what was rejected
docs/applied.md           on-air audit trail: one row per apply
docs/public-readiness.md  the public launch record and re-checks
instance/                 GITIGNORED — a deployment's real data
  settings.json           config, secrets redacted
  character/              character.json, personas/, shows/, schedule.json
  skills/<slug>/SKILL.md  station skills (the DJ's segments)
  blocklist.json          bans and rules
  ids.lock.json           generated: slug -> id, capture record, last applied hash
```

Two directories are called `skills/` and they are different things: the repo-root one holds *agent
procedures*, `instance/skills/` holds *station skills* that go to air.

## How it is verified

Everything is offline. No test needs a station, a network, ssh or credentials — that is why the mock
controller exists, and a test that needs a real station belongs behind a flag, not in `tests/`.

| Check | How |
| --- | --- |
| Model valid | `diff` plus schema validation on `instance/` and `examples/demo/` |
| No instance leak | `sh scripts/check-secrets.sh --all`, plus a scrub grep over **tracked** files |
| Idempotent | `pull` → `diff` → `pull` is clean; `apply` twice changes nothing |
| Quickstart | `tests/test_smoke.py` copies only tracked files to a temp dir, starts the mock, and drives `status` → `pull --allow-lossy` → `validate` → `diff` → `apply` as subprocesses with no ssh and no HOME. If it fails, the README is lying |
| Tier honesty | a tier-1 run refuses `backup` and names the missing schedule; no command silently trims |
| Demo | CI validates `examples/demo/`, so a regression in the schema reference or the scaffold fixture fails there |
| Audit | every apply has a row in `docs/applied.md` and a backup outside the repo |

The suite is the guard against regressions; a live station is the guard against the suite being wrong.
The first real `apply` found three defects the mock could not have found (a backup arity crash, the
character layer never reaching the station, and a read-back that printed real secrets). Expect the same
class of gap in any path that has only ever run against the mock — [PLAN.md](PLAN.md) tracks which those are.

## What this is not

Not a music library or a Navidrome replacement. No binary assets (themes, sfx, jingles, voices) in git.
No auto-apply, no daemon, no cron, no post-commit hook that writes to a station. No GitHub settings
changes. No station code: the controller, Icecast and Liquidsoap are upstream.
