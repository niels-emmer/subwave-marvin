# subwave-marvin

An agent-agnostic toolkit for running a self-hosted **SUB/WAVE** radio station from an agent session.
One Icecast stream, so everyone hears the same track at the same moment; the DJ picks from your own
Navidrome library and talks between tracks. You describe the station in files, review the diff, and push
it — or ask an agent to do it for you.

## Requirements

- `python3` — standard library only, no dependencies, no virtualenv
- A running SUB/WAVE station
- Its admin credentials (`ADMIN_USER` / `ADMIN_PASS`)

## Two tiers

Everything works against the HTTP API. With host access it also reads the station's own state files,
which is where full fidelity lives.

|  | Tier 1: HTTP only | Tier 2: host access |
| --- | --- | --- |
| Needs | base URL + admin credentials | also ssh and `docker exec` |
| Read personas and shows | trimmed | full |
| Read the grid | yes | yes |
| Create / update | only from a complete capture | yes |
| Backup | without the schedule | complete |

Every command prints the tier it is running at. A tier-1 `pull` is always lossy — the API hides fields —
so `validate` fails and `apply` refuses. Host access is needed once, to take a complete capture; after
that, writing from it works at either tier.

## Quickstart

```sh
cp .env.example .env     # base URL, admin credentials, station password
# tier 2 only: also set SUBWAVE_HOST and SUBWAVE_STATE_DIR
python3 scripts/subwave.py status
```

`instance/` holds your station's data and is gitignored. `examples/demo/` is a fictional station in the
same shape, so you can try the whole flow without a station:

```sh
python3 scripts/subwave.py character init --from demo
python3 scripts/subwave.py validate --dir instance
```

## Reading the station

| Command | |
| --- | --- |
| `status` | tier, station, what is on air, and whether the repo is in sync |
| `pull [--force] [--allow-lossy]` | capture the station into `instance/`; `--force` replaces an existing capture, `--allow-lossy` accepts a partial tier-1 one |
| `diff` | drift between `instance/` and the station; exit 1 when it differs |
| `validate [--dir DIR]` | check a capture before applying it; `--dir` points at another station directory |

## Authoring, offline

These edit files only. They never touch the network.

| Command | |
| --- | --- |
| `character init [--from demo\|blank] [--force]` | create the character layer |
| `character new-persona <slug> [--name NAME]` | a persona with all 18 fields |
| `character new-show <slug> [--name NAME] [--persona SLUG]` | a show with all 26 fields |
| `character add-to-grid <slug> --days mon,tue --from 9 --to 12 [--replace] [--clear]` | fill grid cells; `--clear` empties them instead |
| `character edit-persona <slug> --set FIELD=VALUE [--force-value]` | change named fields only |
| `character edit-show <slug> --set FIELD=VALUE [--force-value]` | the same for a show |
| `blocklist list \| block \| unblock \| rm \| rule add\|edit` | bans and rules; `block --type {artist,album,track} --id ID` |
| `skills list \| new <slug> \| show <slug> \| edit <slug> \| rm <slug>` | the DJ's segments |

`--force-value` accepts a value outside the known vocabulary with a loud warning, for a station newer
than the toolkit. A typo is still refused, because it would reach the station.

## Applying

`apply` pushes the capture to the station. **It is a dry run unless you pass `--yes`.**

| Flag | |
| --- | --- |
| `--yes` | perform the writes |
| `--dry-run` | explicit dry run, which is already the default |
| `--force-now` | write even while a show is on air |
| `--no-backup` | write without taking a backup first |
| `--backup-dir DIR` | where that backup goes |
| `--prune` | also delete entities that exist on the station but not in the repo |
| `--force-prune` | delete even when a grid cell or a hosted show depends on it |

## Backup and restore

```sh
python3 scripts/subwave.py backup [--dir DIR] [--with-library]
python3 scripts/subwave.py restore <directory> [--yes] [--force-now]
```

`backup` copies the config, the assets and the blocklist, and writes a `RESTORE.md` saying where each
file goes back. `--with-library` adds `library.db`, which is audio analysis: large, and expensive to
rebuild, so it is skipped by default.

## On air

`onair` talks to the station's MCP server to change what is happening **now**. It is the one imperative
surface: nothing it does is captured, diffed, applied or committed. Every verb that changes air needs
`--yes`; without it the exact call is printed and nothing is sent.

| Command | |
| --- | --- |
| `onair tools \| now \| state \| list {skills,sfx,jingles}` | read-only |
| `onair say <text> [--raw] [--over-track] [--sfx NAME]` | have the DJ speak |
| `onair segment <type>` | run a programmed segment |
| `onair skip [--expect-id ID] [--any-track]` | skip; needs one of those flags, because a skip has no undo |
| `onair request <text>` | request a song, then report the outcome |
| `onair queue <terms> [--pick N]` | search, then queue a result |
| `onair sfx <name>` / `onair jingle <file>` | play one |

## What protects you

The station is live, so the defaults are cautious:

- **`apply` is a dry run** until `--yes`, and prints every step first.
- **`validate` runs before any write** and refuses on an error. A typo'd field is silently dropped by
  the station's shallow merge, so validation is what catches it.
- **Every write is read back.** If a field did not land, `apply` fails loudly and names it.
- **A lossy capture is refused**, because pushing trimmed shows back would strip the fields the API hid.
- **Secrets never print.** `pull` redacts them; the post-apply check names a secret path but never shows
  its value.
- **`--prune` is opt-in and refuses by default** when something depends on the entity: a show in a grid
  cell, a persona hosting a show. The active persona, the last persona and the last show cannot be
  deleted at all. A backup is mandatory, and prune runs only after the writes have verified.
- **`onair skip` needs `--expect-id` or `--any-track`**, and a 401 tells you which credential is missing.

## Layout

```
instance/            your station, gitignored
  settings.json        station config, secrets redacted
  blocklist.json       bans and rules
  skills/<slug>/       the DJ's segments
  character/           identity, personas, shows, the grid
skills/              agent procedures — how an agent works this repo
examples/demo/       a fictional station in the same shape
scripts/subwave.py   the whole tool
```

`instance/skills/` holds **station** skills, the DJ's segments. `skills/` at the root holds **agent**
procedures. They are different things.

## Docs

- `AGENTS.md` — what an agent needs to know first
- `docs/PLAN.md` — design, phases, what is not built yet
- `docs/decision-log.md` — why things are the way they are
- `docs/applied.md` — the on-air audit trail

## Contributing

Run `python3 -m unittest discover -s tests` before you commit; the suite is offline and uses a mock
station. `sh scripts/check-secrets.sh` blocks credentials from entering a commit.
