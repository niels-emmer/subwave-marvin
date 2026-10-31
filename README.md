# subwave-marvin

An agent-agnostic toolkit for running a self-hosted **SUB/WAVE** radio station from an agent session. One
Icecast stream, so everyone hears the same track at once; the DJ picks from your own Navidrome library and
talks between tracks. You describe the station in files, review the diff, and apply it — or ask an agent to.

## Requirements

`python3` (standard library only, no dependencies, no virtualenv), a running SUB/WAVE station, and that
station's admin credentials.

## Two tiers

|  | Tier 1: HTTP only | Tier 2: host access |
| --- | --- | --- |
| Needs | base URL + admin credentials | also ssh and `docker exec` |
| Read personas and shows | trimmed | full |
| Read the grid | yes | yes |
| Backup | **without the schedule** | complete |

Tier 1 cannot read full show definitions — the API returns 7 of a show's 26 fields — and it cannot take a
complete backup, because the station's own export omits the schedule. Both need tier 2. Every command
prints the tier it is running at.

## Setup

```sh
cp .env.example .env
```

Six variables, read from `.env` or the environment:

| Variable | |
| --- | --- |
| `SUBWAVE_BASE_URL` | the station's admin API, e.g. `http://127.0.0.1:7700`; a tunnel is fine |
| `SUBWAVE_ADMIN_USER` | the station's `ADMIN_USER` |
| `SUBWAVE_ADMIN_PASS` | the station's `ADMIN_PASS` |
| `SUBWAVE_STATION_PASSWORD` | the listener password (`settings.privacy.password`) |
| `SUBWAVE_HOST` | tier 2: the ssh host |
| `SUBWAVE_STATE_DIR` | tier 2: the state dir **inside the controller container**, e.g. `/var/sub-wave` |

## Quickstart

```sh
python3 scripts/subwave.py pull              # wrote instance/ (N files + ids.lock.json)
# edit a file under instance/, or use a command from Authoring below
python3 scripts/subwave.py validate          # result ok (N personas, N shows, N/N cells), 0 warning(s)
python3 scripts/subwave.py apply             # dry run: N step(s) prepared, 0 requests sent
python3 scripts/subwave.py apply --yes       # N step(s) written; ids.lock.json updated
```

`apply` prints every step before it writes, and sends nothing until `--yes`.

## Commands

### Reading

| Command | Purpose | Example |
| --- | --- | --- |
| `status` | tier, station, what is on air, repo sync | `status` |
| `pull` | capture the station into `instance/` | `pull --force` |
| `diff` | drift vs the station; exit 1 when it differs | `diff` |
| `validate` | check a capture before applying | `validate --dir examples/demo` |

`--force` replaces a capture, `--allow-lossy` accepts a partial tier-1 one, `--dir` points elsewhere.

### Writing

`apply` dry-runs by default and prints each step; `--yes` performs the writes. `--force-now` writes while a
show is on air; `--no-backup` skips the pre-apply backup and `--backup-dir` moves it. `--prune` deletes entities on the station but not in the repo; `--force-prune` allows that when something depends on them.

| Command | Purpose | Example |
| --- | --- | --- |
| `apply` | push the capture to the station | `apply` then `apply --yes` |

### Safety net

| Command | Purpose | Example |
| --- | --- | --- |
| `backup` | config, assets and blocklist, plus a `RESTORE.md` | `backup --dir ~/backups` |
| `restore` | put a backup back: state files, then assets | `restore <dir> --yes` |

`--with-library` adds `library.db`, which is large and rebuildable, so it is skipped by default.

### Authoring — local file edits, no network

| Command | Purpose | Example |
| --- | --- | --- |
| `character init` | create the character layer | `character init --from demo` |
| `character new-persona` / `new-show` | a persona (18 fields) or a show (26) | `character new-show <slug> --persona <persona>` |
| `character add-to-grid` | fill grid cells, or `--clear` them | `character add-to-grid <show> --days mon --from 9 --to 12` |
| `character edit-persona` / `edit-show` | change named fields only | `character edit-persona <persona> --set warmth=8` |
| `skills` | the DJ's own segments | `skills new late-shift`, `skills edit <slug> --brief "..."` |
| `blocklist` | bans | `blocklist block --type artist --id <id>` |
| `blocklist rule` | rules | `blocklist rule add --label "No opera" --field genre --value Opera` |

`character init` takes `--from demo|blank` and `--force`; `add-to-grid` takes `--replace` to take over
occupied cells; `edit-*` take `--force-value` for a value this toolkit does not know. `skills` also has
`list`/`show`/`rm`; `blocklist` also has `list`/`unblock`/`rm`; `rule` also has `edit`.

### On air — imperative, immediate

| Command | Purpose | Example |
| --- | --- | --- |
| `onair tools` / `now` / `state` | the catalogue, what is on air, the queue and history | `onair now` |
| `onair say` | have the DJ speak (`--raw`, `--over-track`, `--sfx`) | `onair say "Back after this." --yes` |
| `onair segment` | run a programmed segment | `onair segment hourly --yes` |
| `onair skip` | skip the current track (`--expect-id`, `--any-track`) | `onair skip --expect-id <id> --yes` |
| `onair request` | request a song, then report the outcome | `onair request "something like Portishead" --yes` |
| `onair queue` | search, then queue a result | `onair queue portishead --yes --pick 2` |
| `onair sfx` / `onair jingle` | play one | `onair sfx airhorn --yes` |
| `onair list` | skills, sfx or jingles | `onair list skills` |


## Declarative vs imperative

Everything except `onair` describes the station: it is captured, diffed, validated and applied
deliberately. `onair` changes the broadcast now — it is never part of `apply` or `diff`, nothing it does
is captured, and nothing it does enters git.

## Safety model

- **A lossy capture is refused** — pushing trimmed shows back would strip the fields the API hid.
- **A payload holding a redaction placeholder is refused**, so a credential is never overwritten with it.
- **Writing while a show is on air is refused** unless `--force-now`.
- **A backup is mandatory before writes and before any prune** — a delete has no undo.
- **Prune refuses anything with dependents**, and never deletes the active or last persona, or last show.
- **Every air-changing verb needs `--yes`**; `skip` also needs `--expect-id` or `--any-track`.
- **`validate` runs before any write, and every write is read back** — a field that did not land is named.

## What it does not do

- **Avatars and imaging media** — binary; `backup` covers them, authoring does not.
- **Library operations** — tagging, playlist building, analysis. That is Navidrome's and the station's job.
- **Anything that bypasses the station's `tool.mjs` trust boundary** — tool code stays quarantined until a
  human reviews it and trusts its sha256.

## Layout

```
instance/          your station — gitignored; character/ holds identity, personas,
                   shows and the grid, skills/<slug>/ the station's own DJ segments
skills/            agent procedures — how an agent works this repo
examples/demo/     a committed fictional station, same shape as instance/
scripts/subwave.py the whole tool
```

## Where to read more

`AGENTS.md` for agents · `docs/PLAN.md` design · `docs/decision-log.md` why · `docs/applied.md` what aired.
