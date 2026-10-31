# AGENTS.md — SUB/WAVE toolkit

A toolkit for running a **SUB/WAVE** radio station from an agent session: author the station's
character (personas, shows, schedule, house rules), read what is actually live, and push reviewed
changes. Agent-agnostic — Claude Code, Codex, OpenCode, or anything else that reads `AGENTS.md`.
Nothing here belongs to one station: your deployment's data lives in `instance/`, which is gitignored,
and `examples/demo/` shows the shapes.

## Mental model

It is a radio station, not a playlist. One Icecast stream; every listener hears the same track at the
same moment; there is no skip and no per-listener queue. A **show** is a programme — persona + brief +
genre/mood/era filters — and the **7×24 grid** decides which show owns each wall-clock hour. A
**persona** is an LLM DJ that talks between tracks; its `soul` field *is* the system prompt. Everything
runs live, so a config change alters the next track and the next talk segment, not an offline artifact.

## Two capability tiers — announce, never degrade silently

| | Tier 1: HTTP only | Tier 2: host access |
| --- | --- | --- |
| Needs | base URL + admin credentials | + ssh and `docker exec` |
| Create / update | yes | yes |
| Read personas, shows | **trimmed** | full |
| Read the grid | yes | yes |
| Backup | **no schedule** | complete |

Every operation says which tier it is running at. Tier 1 is portable and lossy; do not pretend otherwise.

**Tier 1 cannot read full show definitions.** `/api/schedule` returns shows cut to 7 of 26 fields and
there is no `GET /shows`. So `apply` must only ever send whole objects built from the repo — never
read-modify-write from a trimmed read, which would silently drop the fields it cannot see.

## Where config actually lives

Full fidelity lives in the state dir on the host, as two JSON documents:

- `settings.json` — the whole station config. `personas[]` and `activePersonaId` are **inside it**, so
  persona config and station config are one document.
- `schedule.json` — `{shows[], schedule, override}`; `schedule` maps day `"0".."6"` (JS `getDay()`,
  0 = Sunday) to 24 show ids.

They are usually root-owned, so plain ssh/scp fails. Read them through the controller container:

```sh
ssh "$SUBWAVE_HOST" 'docker exec sub-wave-controller cat "$SUBWAVE_STATE_DIR/settings.json"'
ssh "$SUBWAVE_HOST" 'docker exec sub-wave-controller cat "$SUBWAVE_STATE_DIR/schedule.json"'
```

The container name is install-specific; the image default is `sub-wave-controller`. Runtime and derived
files in the same dir (`library.db*`, `secrets.env`, `icecast*`, `liquidsoap_*.txt`, archives, logs) are
never copied and never hand-edited.

## The documented API is not the config API

`GET /api/connect/openapi.json` is a curated subset (auth required) with **no config writes**, and the
read endpoints are presentation views: `/api/personas` returns 4 fields per persona even as admin,
`/api/schedule` trims shows, and admin `GET /api/settings` is a UI summary rather than `settings.json`.

Real write routes, absent from the OpenAPI export:

| Route | Semantics |
| --- | --- |
| `POST /api/settings` | admin, **shallow partial merge**. Send whole top-level sections: `{"personas":[…]}` replaces the array, and a partial persona loses fields. |
| `POST /api/shows` | admin, `{"show":{…}}`, **upsert by `show.id`**; no id creates a show (409 at the show cap). |
| `PUT /api/schedule` | admin, **replaces the whole grid** — always send all 7×24 cells. |
| `POST` / `DELETE /api/schedule/override` | admin, one-off programme override. |
| `POST /api/personas/import`, `GET /api/personas/:id/export`, `*/personas/:id/avatar`, `DELETE /api/personas/:id`, `DELETE /api/shows/:id` | admin; the deletes are destructive. |
| `GET /api/backup/export` | admin, large zip — see below. |

Auth classes: public (none), station (`x-station-auth` listener password), admin (HTTP Basic). Ids are
server-generated (`p_xxxxxx` personas, `s_xxxxxx` shows), so a slug→id lockfile is what stops a rename
from creating a duplicate.

## MCP is control-only — settled

`/api/mcp` exposes 21 tools: reads, listener requests, and on-air control. **Zero config writers** — no
persona, show, schedule or settings tool exists. Config goes through the admin HTTP API. Do not
re-litigate this from the published manual, which contradicts itself; `tools/list` is the truth.

## A backup does not contain the schedule

`GET /api/backup/export` is a ~95 MB zip holding `settings.json`, `library.db`, skills, sfx, jingles,
avatars and themes — and **no `schedule.json`**. Personas survive because they live in settings.json;
every show and the whole grid do not. A backup that silently omits the programming is worse than none:
include the schedule (tier 2, copy `schedule.json` as well) or refuse and say why (tier 1).

## Safety

- The station is **live**. Check what is on air before applying and prefer an off-air window.
- **Not every setting applies live.** `privacy.listenerAuth` and `stream.maxListeners`/`opus`/`aac` take
  effect only on a **mixer restart**, where the controller regenerates the Icecast config; one restart
  costs seconds of stream interruption, so bundle such changes. Passwords and the rest apply live.
- Never commit instance data. `instance/` and `.env` are gitignored, and `scripts/check-secrets.sh`
  blocks live values from entering a commit.
- Deletes run only when the human asks, never as a side effect of a sync.
- Never run a second stack against the same state dir.

## Commands

Run `git config core.hooksPath .githooks` once after cloning: it enables the pre-commit secret gate,
which agents otherwise never turn on.

```sh
sh scripts/check-secrets.sh          # commit gate: tracked files
sh scripts/check-secrets.sh --all    # also untracked-but-not-ignored
```

Planned, not implemented yet — design in `docs/PLAN.md`:

```sh
scripts/subwave.py status            # which tier, which station, what is on air
scripts/subwave.py pull              # live -> instance/ (secrets redacted)
scripts/subwave.py diff              # read-only drift report
scripts/subwave.py apply --dry-run   # print payloads, send nothing
scripts/subwave.py backup            # refuses at tier 1; includes the schedule at tier 2
```

## Layout

`instance/` is this deployment and is gitignored: `settings.json` plus a `character/` layer
(`character.json` for identity and house rules, `personas/*.json`, `shows/*.json`, `schedule.json`).
`examples/demo/` is a committed fictional station in the same shape, so the repo is testable with no
live station. `docs/PLAN.md` holds the design and phases, `docs/decision-log.md` the decisions, and
`docs/applied.md` the on-air audit trail.
