# Decision log

Append-only, newest first. Each entry: **Date / Decision / Why / Rejected alternatives.**
Never rewrite an entry; supersede it with a new one and mark the old one `superseded by Dn`.

---

## D9 — The toolkit must capture the schedule itself

- **Date:** 2026-10-31
- **Status:** decided-by-orchestrator
- **Decision:** `backup` does not delegate to the built-in export. At tier 2 it writes the zip **plus**
  `schedule.json`; at tier 1 it refuses and names what is missing. `pull` captures `schedule.json` from
  the host for the same reason.
- **Why:** the built-in `GET /api/backup/export` is a ~95 MB zip with `settings.json`, `library.db`,
  skills, sfx, jingles, avatars and themes — and **no `schedule.json`**. Personas survive because they
  live in settings.json; every show and the entire grid do not. Full show definitions cannot be read back
  over HTTP at all (`/api/schedule` trims to 7 of 26 fields, there is no `GET /shows`).
- **Rejected alternatives:** treating the API export as "the backup" (silently loses all programming);
  reconstructing shows from the trimmed read (19 fields are simply absent).

## D8 — The character layer is pluggable

- **Date:** 2026-10-31
- **Status:** decided-by-orchestrator
- **Decision:** a *character* is the themed bundle that makes a station itself — identity, locale,
  timezone, theme, house rules, persona roster, show briefs and the grid. It lives in
  `instance/character/` as small files, so a different character is a different directory, not a fork of
  the code.
- **Why:** the repo is a toolkit for any station; the theme is a user choice, not a product fact. It also
  keeps themed content (and any IP in it) out of a public repo by construction.
- **Rejected alternatives:** hard-coding one theme; keeping personas and shows inside one monolithic
  settings document (unreviewable diffs).

## D7 — Tier 2 (host access) is an accepted requirement

- **Date:** 2026-10-31
- **Status:** decided-by-orchestrator
- **Decision:** ssh + `docker exec` is a supported, first-class capability, not a workaround. Tier 1
  remains fully portable, and every command announces which tier it is at.
- **Why:** full-fidelity reads and a complete backup are impossible over HTTP alone (see D9). Accepting
  host access buys correctness; keeping tier 1 keeps the toolkit usable by someone who will not grant it.
- **Rejected alternatives:** HTTP-only (lossy and dishonest about it); requiring host access (kills the
  portable case).

## D6 — Public repo, instance data gitignored

- **Date:** 2026-10-31
- **Status:** decided-by-orchestrator
- **Decision:** the repository will be public. All instance-specific data and PII live in gitignored
  files (`instance/`, `.env`), with anonymised `.example` equivalents committed. Tracked files carry zero
  station facts.
- **Why:** a public toolkit cannot ship one deployment's hostnames, credentials, roster or themed
  content. Scrub-after-the-fact does not work: git history keeps it.
- **Rejected alternatives:** scrubbing before publishing (history survives); a private repo (the point is
  a toolkit others can clone).

## D5 — Secrets never enter the repo

- **Date:** 2026-10-31
- **Status:** decided-by-orchestrator
- **Decision:** Credentials live only in a gitignored `.env` or the OS keychain. `pull` redacts secret
  paths to `"__REDACTED__"` on the way in; `apply` re-hydrates them from the environment at the moment of
  writing. `scripts/check-secrets.sh` plus the pre-commit hook block a leak, and CI runs the same check.
- **Why:** the repo talks to a live broadcast and will be public. A committed credential cannot be
  un-published by a later commit.
- **Rejected alternatives:** encrypting secrets in-repo (the key has to live somewhere, and the blast
  radius is unchanged); relying on `.gitignore` alone (one `git add -f` defeats it).

## D4 — `listenerAuth` is self-contained in settings

- **Date:** 2026-10-31
- **Status:** decided-by-orchestrator
- **Decision:** Flipping `privacy.listenerAuth` is a settings change plus one mixer restart. The
  controller writes the Icecast listener-auth file; the next broadcast/mixer restart regenerates the
  Icecast config with per-mount URL auth pointing back at the controller. No `icecast.xml`, compose or
  reverse-proxy edit. `stream.maxListeners` rides the same restart path.
- **Why:** verified against a live station. It means the change is reviewable like any other setting
  instead of being an undocumented host edit.
- **Rejected alternatives:** hand-editing the generated `icecast.xml` (overwritten on restart); changing
  the reverse proxy (it already refuses the listener-auth path).

## D3 — MCP is control-only

- **Date:** 2026-10-31
- **Status:** decided-by-orchestrator — settled, do not re-litigate
- **Decision:** `/api/mcp` exposes 21 tools: reads, listener requests, and on-air control. There are
  **zero** config writers. Personas, shows, schedule and settings go through the admin HTTP API.
- **Why:** verified with `initialize` + `tools/list` against a live endpoint. The published manual
  contradicts itself on the count, so `tools/list` is the only truth.
- **Rejected alternatives:** treating the manual as authoritative; assuming a config surface exists
  because the tool list is long.

## D2 — Read state files, write the admin API

- **Date:** 2026-10-31
- **Status:** decided-by-orchestrator
- **Decision:** `pull`/`diff` read `settings.json` and `schedule.json` from the host state dir via
  `docker exec`. `apply` writes through the admin HTTP API (`POST /api/settings`, `POST /api/shows`,
  `PUT /api/schedule`).
- **Why:** the read endpoints are presentation views, not the config document — 4 fields per persona, 7 of
  26 per show, and a 19-key UI summary from admin `GET /api/settings`. The documented OpenAPI export has
  no config writes at all.
- **Rejected alternatives:** round-tripping config through the documented API (silently truncates
  personas and shows); the full backup export (~95 MB, library included, no schedule); editing state
  files directly (root-owned, rewritten by the controller).

## D1 — The repo is the source of truth

- **Date:** 2026-10-31
- **Status:** decided-by-orchestrator
- **Decision:** Changes are authored here, reviewed as a diff, then applied with explicit verbs: `pull`
  (live → repo), `diff` (read-only drift), `apply` (repo → live). Nothing auto-applies: no daemon, no
  cron, no post-commit hook that writes to a station.
- **Why:** the station is live and a bad config change is audible. Review has to happen before the write,
  and drift has to be visible without touching anything.
- **Rejected alternatives:** editing the live instance and treating it as authoritative; a scheduled
  auto-sync (drift would go on air unattended).

---

## Open questions (not decided)

Tracked in `docs/PLAN.md` §12. Do not resolve these in code.

- **Container name** — add `SUBWAVE_CONTAINER`, or document the image default?
- **Tier-1 schedule writes** — allow `PUT /api/schedule` at tier 1, or refuse because shows cannot be read?
- **Playlist anchoring** — how does the toolkit learn Navidrome playlist ids?
- **Deploy artifacts** — ship a compose file, or point at upstream?
- **Demo as template** — is `examples/demo` illustrative only, or the thing users copy?
- **Skill surface** — which tasks get a skill first?
