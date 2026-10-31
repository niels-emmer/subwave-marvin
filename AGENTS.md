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
| Create / update | only from a complete capture | yes |
| Read personas, shows | **trimmed** | full |
| Read the grid | yes | yes |
| Backup | **no schedule** | complete |

Every operation says which tier it is running at. Tier 1 is portable and lossy; do not pretend
otherwise.

A tier-1 `pull` is **always** lossy — the API cannot return the 14 persona or 19 show fields it hides —
so `validate` fails and `apply` refuses. Host access is therefore needed **once**, to take a complete
capture; after that, writing from that capture works at either tier.

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
ssh "$SUBWAVE_HOST" "docker exec sub-wave-controller cat '$SUBWAVE_STATE_DIR/settings.json'"
ssh "$SUBWAVE_HOST" "docker exec sub-wave-controller cat '$SUBWAVE_STATE_DIR/schedule.json'"
```

`SUBWAVE_STATE_DIR` is the path **inside the controller container** — the bind mount, usually
`/var/sub-wave` — not the host path. Single-quoting the outer ssh argument would leave it to the remote
shell, which does not have the variable, and the path collapses to `/settings.json`. The container name is
install-specific too; the image default is `sub-wave-controller`. Runtime and derived files in the same dir
(`library.db*`, `secrets.env`, `icecast*`, `liquidsoap_*.txt`, archives, logs) are never copied and never
hand-edited.

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
every show and the whole grid do not. So `backup` copies the config **and** those assets, skips
`library.db` unless `--with-library`, and writes a `RESTORE.md` saying where each file goes back.

## Safety

- The station is **live**. Check what is on air before applying and prefer an off-air window.
- **Not every setting applies live.** `privacy.listenerAuth` and `stream.maxListeners`/`opus`/`aac` take
  effect only on a **mixer restart**, where the controller regenerates the Icecast config; one restart
  costs seconds of stream interruption, so bundle such changes. Passwords and the rest apply live.
- Never commit instance data. `instance/` and `.env` are gitignored, and `scripts/check-secrets.sh`
  blocks live values from entering a commit.
- **`apply` runs `validate` first and refuses on any error.** A typo'd field is silently dropped by a
  shallow merge rather than rejected, so validation is the only thing standing between a typo and a
  live config change. Unknown fields are warnings (a newer server may add them); missing fields are errors.
- **`character edit-persona` / `edit-show` change only the fields you name**, leave every other field's
  value and position alone, and never touch an `id` or a slug. They refuse an unknown field (listing the
  real ones) and a wrong type or enum rather than coercing it, and restore the file if the result would
  fail `validate`. Enum sets are copied from upstream (`controller/src/schemas/persona.ts` and
  `show.ts`) in the product's ladder order, not inferred from the demo; `--force-value` lets a value this
  tool does not know through with a loud warning, for a station newer than the toolkit. Values are
  checked at edit time; `validate` itself stays permissive about a value a newer server may know.
- **`apply` refuses a lossy capture.** `pull` records `capture.lossy` in `ids.lock.json`; a
  tier-1 `--allow-lossy` pull sets it, and `apply` then refuses outright, because pushing 7-field
  shows back would strip 19 fields per show. This is a safety property, not a warning: do not relax it.
- `apply` also refuses when a payload still holds `__REDACTED__` (a secret was not rehydrated), when
  the station is mid-show without `--force-now`, and at tier 1 without `--no-backup`. It never deletes.
- Deletes run only when the human asks, never as a side effect of a sync.
- Never run a second stack against the same state dir.

## Commands

Run `git config core.hooksPath .githooks` once after cloning: it enables the pre-commit secret gate,
which agents otherwise never turn on.

```sh
sh scripts/check-secrets.sh          # commit gate: tracked files
sh scripts/check-secrets.sh --all    # also untracked-but-not-ignored
python3 -m unittest discover -s tests  # offline: the mock station is in-process

python3 scripts/subwave.py status    # tier, station, on-air state, repo sync
python3 scripts/subwave.py pull      # live -> instance/, secrets redacted (--force replaces)
python3 scripts/subwave.py diff      # drift vs live, exit 1 when drifted
python3 scripts/subwave.py backup    # config + assets, outside the repo
python3 scripts/subwave.py validate  # check instance/ before applying (--dir DIR for any station)
python3 scripts/subwave.py character init --from demo   # scaffold a character layer (no network)
python3 scripts/subwave.py character new-persona|new-show|add-to-grid   # author personas, shows, grid
python3 scripts/subwave.py character edit-persona|edit-show <slug> --set FIELD=VALUE  # change named fields only
python3 scripts/subwave.py apply     # DRY RUN by default; --yes performs the writes
```

## Layout

`instance/` is this deployment and is gitignored: `settings.json` (secrets redacted),
`character/character.json` for identity and house rules, `character/personas/*.json`,
`character/shows/*.json`, `character/schedule.json` (the grid keyed by slug), and
`ids.lock.json` — generated, never hand-edited, holding the slug→id map, the `capture`
record `apply` checks, and the hash of the last applied payload.

`examples/demo/` is a committed fictional station in the same shape, so the repo is testable
with no live station. `docs/PLAN.md` holds the design and phases, `docs/decision-log.md` the
decisions, and `docs/applied.md` the on-air audit trail.
