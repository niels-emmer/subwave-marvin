# PLAN — subwave-marvin

A toolkit for operating any **SUB/WAVE** radio station from an agent session. It is not one station's
config: the repo is public-facing and agent-agnostic, and a deployment's real data lives in a gitignored
`instance/`.

Status: **P0 done** (this deployment captured). The tool itself is P1. Nothing has been applied to a
live station by this repo.

---

## 1. The two constraints

**C1 — the repo will be public.** Every instance-specific fact and all PII live in gitignored files, with
anonymised `.example` equivalents committed. That includes station identity, hostnames, credentials,
persona and show definitions, and any themed character content.

**C2 — agent-agnostic.** Clone into Claude Code, Codex, OpenCode or anything else that reads `AGENTS.md`
and chat to your own station. No tool-specific assumptions; skills, scripts and docs must work for any
agent and any install.

## 2. The finding that shapes the design

The documented API is not the config API. Verified against a live station:

| Endpoint | Actually returns |
| --- | --- |
| `GET /api/personas` | 4 fields per persona (`id, name, tagline, avatar`) — even with admin auth |
| `GET /api/schedule` | the grid, plus shows cut to **7 of 26** fields |
| `GET /api/settings` (admin) | a **19-key UI summary**, not the 52-key `settings.json` |
| `GET /api/connect/openapi.json` | curated subset, auth required, **no config writes** |
| `GET /api/backup/export` | ~95 MB zip, **no `schedule.json`** |
| `GET /shows` | **does not exist** |

So full show definitions are unreadable over HTTP at all. Only the host filesystem has them. Every design
decision below follows from that.

## 3. Two capability tiers

| | Tier 1: HTTP only | Tier 2: host access |
| --- | --- | --- |
| Needs | `SUBWAVE_BASE_URL` + admin credentials | + `SUBWAVE_HOST`, `SUBWAVE_STATE_DIR`, ssh, `docker exec` |
| Create / update | yes | yes |
| Read personas | trimmed (4 fields) | full |
| Read shows | trimmed (7 of 26) | full |
| Read the grid | yes | yes |
| Backup | zip only — **no schedule** | zip **+ `schedule.json`** |
| Portable | yes | no (needs host access) |

Two rules the tool must honour:

1. **Announce the tier.** `status` prints which tier it is at and what that costs. Never degrade silently.
2. **Never read-modify-write at tier 1.** `apply` builds whole objects from the repo and sends those. A
   trimmed read merged into a write would silently drop the 19 fields it cannot see.

## 4. Target layout

```
AGENTS.md                 agent-agnostic, zero instance facts
README.md
.env.example              SUBWAVE_BASE_URL, _ADMIN_USER, _ADMIN_PASS, _STATION_PASSWORD,
                          _HOST (tier 2), _STATE_DIR (tier 2)
.gitignore                ignores instance/ and .env
scripts/subwave.py        the tool                                    (P1)
skills/<name>/SKILL.md    agent skills                               (P4)
examples/demo/            committed fictional station, same shape as instance/
instance/                 GITIGNORED — this deployment
  settings.json
  character/character.json
  character/personas/*.json
  character/shows/*.json
  character/schedule.json
docs/{PLAN,decision-log,applied}.md
```

## 5. The character layer

A **character** is the themed bundle that makes a station itself, and it is pluggable: swap the directory
and you have a different station on the same software.

| File | Holds | Server field it maps to |
| --- | --- | --- |
| `character/character.json` | `name`, `description`, `locale`, `timezone`, `theme`, `houseRules` | `station`, `stationDescription`, `locale`, `timezone`, `theme.active`, `djHouseRules` |
| `character/personas/*.json` | one persona per file (18 fields, `soul` is the system prompt) | `settings.personas[]` |
| `character/shows/*.json` | one show per file (26 fields) | `schedule.shows[]` |
| `character/schedule.json` | `{schedule, override}` — the 7×24 grid by slug | `schedule.schedule`, `schedule.override` |

The split is a repo convenience: the server keeps personas inside `settings.json` and shows inside
`schedule.json`. `pull` splits them out; `apply` reassembles them.

`examples/demo/` is a committed fictional station in exactly this shape (invented hosts, placeholder ids),
so the repo is testable with no live station and no instance data.

## 6. The tool

```
scripts/subwave.py status            # tier, station, on-air state, what this tier can and cannot do
scripts/subwave.py pull [--check]    # live -> instance/ (secrets redacted)
scripts/subwave.py diff              # read-only drift report
scripts/subwave.py apply [--dry-run] # repo -> live, whole objects only
scripts/subwave.py backup <dir>      # tier 2 includes schedule.json; tier 1 refuses
```

- **`pull`** reads `settings.json` + `schedule.json` from the host (tier 2) or the trimmed API (tier 1,
  and says what it could not read). Redacts secret paths to `"__REDACTED__"`, splits personas and shows
  into per-slug files, regenerates `ids.lock.json`.
- **`diff`** compares repo-desired against live, per section and per entity. Never writes.
- **`apply`** is idempotent and ordered: personas → shows → settings sections → grid last, because shows
  reference personas and the grid references shows. Secret-preserving merge keeps live credentials when
  a section containing them is sent. Deletes are never part of `apply`.
- **`backup`** exists because the built-in export silently loses the programming: tier 2 = zip +
  `schedule.json`; tier 1 = refuse with the reason, rather than hand back an incomplete backup.
- **`ids.lock.json`** maps slug → server id (server ids are generated: `p_xxxxxx`, `s_xxxxxx`). It is
  generated, never hand-edited, and is what keeps a rename from creating a duplicate.

## 7. Schema and validation

Python 3 stdlib only — no third-party deps, so the toolkit runs anywhere an agent already is.

- `tools/schema.py` + a field spec: required keys per entity, enums for `frequency`, `scriptLength`,
  `linkStyle`, `vocals`, `moods` (validated against the station's own mood vocabulary), `energies`,
  numeric ranges for `humour`/`warmth`/`localColour`.
- Grid: exactly 7 day keys, 24 cells each, every cell resolving to a known show slug.
- Cross-references: `show.personaId` and `guestPersonaIds` exist; `activePersonaId` exists.
- **Unknown fields are an error**: a field the server has and we do not model would be deleted on merge.
- `"__REDACTED__"` is legal in `instance/` and never legal in an apply payload.

## 8. Phases

| Phase | Work | Exit criteria |
| --- | --- | --- |
| **P0 — capture** *(done)* | `.gitignore` first, then verbatim `settings.json` + `schedule.json` into `instance/` | instance files parse; 6 shows; 168/168 cells; `git status` and `git ls-files` never mention `instance/` |
| **P1 — read-only tool** *(done)* | `status`, `pull`, `diff`, `backup`, tier detection, unit tests | `diff` reports clean straight after `pull`; tier 1 refuses or states exactly what it loses; `python3 -m unittest discover -s tests` green |
| **P2 — character authoring** *(core done)* | character layer files, slug↔id lockfile, splitting/reassembly, `validate` | `pull` round-trips `instance/` byte-identically (modulo redaction); `validate` catches a typo'd field before it can be merged away |
| **P3 — apply** *(done, mock-verified)* | `validate` gate, ordered writes, dry run by default, lossy/sentinel/mid-show/backup guards, id assignment | proven against `tests/mock_station.py`: dry run sends nothing, shows sent whole, grid last, second apply a no-op. **Not yet run against the live station** |
| **P4 — skills** *(merged, unrehearsed)* | `skills/<name>/SKILL.md`: new show, diagnose a host, backup | an agent completes each task from the skill alone, at the tier it announces — **not yet rehearsed end to end** |
| **P5 — public readiness** | scrub, LICENSE, branch protection, repo settings | **human only**; the agent prepares a checklist and changes nothing on GitHub |

**What remains.** P5 public readiness (scrub review, LICENSE, branch protection, repo settings) is
human-only and untouched. P2's mechanics are done, but authoring a brand-new persona from scratch —
rather than editing a pulled one — has no end-to-end rehearsal. P4's skills are merged and consistent
with the tool, but nobody has yet completed a task from a skill alone. And the write path has never
been exercised against a real station: everything above is mock-verified.

**P3 is done against the mock and has never touched the live station.** `apply` refuses a lossy
capture outright, refuses a payload still holding `__REDACTED__`, refuses mid-show without
`--force-now`, and refuses at tier 1 without `--no-backup`. It sends whole objects only, writes
personas and settings before shows and the grid last, and never deletes (`--prune` is reserved and
unimplemented).

**P1 is done, and it does not write.** It ships `status`, `pull`, `diff` and `backup`. It does not
apply anything, does not create, update or delete a persona, show, setting or grid cell, and cannot
restore a backup. `apply` is P3. `backup` only copies files off the host, and refuses at tier 1 rather
than hand back something that cannot restore the programming.

## 9. Verification

| Check | How |
| --- | --- |
| Model valid | `scripts/subwave.py diff` + schema validation on `instance/` and `examples/demo/` |
| No instance leak | `sh scripts/check-secrets.sh --all`, plus a scrub grep for hostnames, IPs, ids, character names over **tracked** files |
| Idempotent | `pull` → `diff` → `pull` is clean; `apply` twice changes nothing |
| Quickstart | `tests/test_smoke.py` copies only tracked files to a temp dir, starts the mock, and drives `status` → `pull --allow-lossy` → `validate` → `diff` → `apply` as subprocesses with no ssh and no HOME. If it fails, the README is lying |
| Tier honesty | tier-1 run refuses `backup` and names the missing schedule; no command silently trims |
| Audit | every apply has a row in `docs/applied.md` and a backup outside the repo |

## 10. Secrets and instance data

- Repo: never. `instance/` and `.env` are gitignored; `.env.example` carries key names only.
- `scripts/check-secrets.sh` (pre-commit + CI) blocks key formats, JSON secret keys, and live values from
  `SUBWAVE_ADMIN_PASS` / `SUBWAVE_STATION_PASSWORD` / `ANTHROPIC_API_KEY` when set.
- `pull` redacts secret paths on the way in (`privacy.password`, `llm.apiKey`, `llm.fallback.apiKey`,
  `tts.cloud.*`, `search.apiKey`, `embedding.apiKey`, `scrobble.*`); `apply` re-hydrates from the
  environment at write time.
- The station password is `settings.privacy.password` — instance data, never tracked.

## 11. Our deployment

This deployment is captured in `instance/` (gitignored) and is deliberately not described here: no
station name, host, or character content appears in tracked files. It is the first user of the toolkit,
not its subject. Station-specific choices — including which persona hosts which show — live in
`instance/character/` and are reviewed there.

## 12. Open questions

1. **Container name.** Tier 2 assumes `docker exec sub-wave-controller`. That is the image default, not a
   guarantee. Add `SUBWAVE_CONTAINER` to `.env.example`, or document the assumption?
2. **Tier-1 apply and the grid.** `PUT /api/schedule` needs all 168 cells, and tier 1 can read the grid —
   but should tier 1 refuse schedule writes anyway, since it cannot verify the shows it points at?
3. **Playlist anchoring.** Shows can reference Navidrome playlist ids, and nothing in the repo knows how
   to learn them. Navidrome API with the station's own credentials, or a manual list?
4. **Deploy artifacts.** Does the toolkit ship a compose file for new installs, or point at upstream?
5. **Character portability.** Should `examples/demo` double as a starting template users copy into
   `instance/`, or stay purely illustrative?
6. **Skill surface.** Which tasks get a skill first: new show, diagnose a host, backup, schedule review?

## 13. Non-goals

Not a music library or a Navidrome replacement. No binary assets (themes, sfx, jingles, voices) in git. No
on-air control (that is MCP's job). No auto-apply, no daemon, no pruning of live entities, no GitHub
settings changes.
