# subwave-marvin

A toolkit for running a self-hosted **SUB/WAVE** radio station — open-source personal internet radio —
from an agent session: one Icecast stream, an AI DJ that picks from your own Navidrome library and talks
between tracks.

It is for one person who already runs SUB/WAVE and would rather talk to their station than click through
an admin UI: "make a new show", "why does this host talk so much", "take a backup".

**Agent-agnostic**, concretely: the entry point is `AGENTS.md`, not a vendor API. Clone the repo, open it
in Claude Code, Codex, OpenCode or anything else that reads `AGENTS.md`; the agent gets the same rules,
runs the same scripts, and loads the same procedures from `skills/`.

## Quickstart

1. `git clone https://github.com/niels-emmer/subwave-marvin.git`
2. `cp .env.example .env`, then fill it in: base URL, admin credentials, station password.
3. `python3 scripts/subwave.py status` — prints the tier, the station, and what is on air.
4. Ask your agent for what you want.

`instance/` is gitignored, so your station's real data never enters the repo.

## Two tiers

| | Tier 1: HTTP only | Tier 2: host access |
| --- | --- | --- |
| Needs | base URL + admin credentials | also ssh and `docker exec` |
| Reads | trimmed: shows lose fields | full fidelity |
| Backup | no schedule | complete |

Tier 1 is portable and lossy; tier 2 — the same clone with `SUBWAVE_HOST` and `SUBWAVE_STATE_DIR` set —
is what a complete backup needs.

**The station is live.** `apply` dry-runs by default; `--yes` writes to the running station.

`AGENTS.md` for agents · `docs/PLAN.md` for the design · `skills/` for the procedures.
