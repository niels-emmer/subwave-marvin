# Decision log

Append-only, newest first. Each entry: **Date / Decision / Why / Rejected alternatives.**
Never rewrite an entry; supersede it with a new one and mark the old one `superseded by Dn`.

---

## D5 — Secrets never enter the repo

- **Date:** 2026-10-31
- **Status:** decided-by-orchestrator
- **Decision:** Credentials live only in a gitignored `.env` or the macOS Keychain. `pull` redacts
  secret paths to `"__REDACTED__"` on the way in; `apply` re-hydrates them from the environment at the
  moment of writing. `scripts/check-secrets.sh` plus the pre-commit hook block a leak, and CI runs the
  same check.
- **Why:** the repo talks to a live broadcast and is private, not secret. A committed station password
  or API key cannot be un-published by a later commit.
- **Rejected alternatives:** encrypting secrets in-repo (key has to live somewhere, and the blast radius
  is unchanged); relying on `.gitignore` alone (one `git add -f` defeats it).

## D4 — `listenerAuth` is self-contained in settings

- **Date:** 2026-10-31
- **Status:** decided-by-orchestrator
- **Decision:** Flipping `privacy.listenerAuth` is a settings change plus one mixer restart. The
  controller writes `state/icecast_listener_auth.txt`; the next broadcast/mixer restart regenerates the
  Icecast config with per-mount URL auth pointing at `POST /listener-auth`. No `icecast.xml`, compose, or
  Caddy edit. `stream.maxListeners` rides the same restart path (`ICECAST_MAX_CLIENTS`).
- **Why:** verified against the live station. It means the change is reviewable in this repo like any
  other setting, instead of being an undocumented host edit.
- **Rejected alternatives:** hand-editing the generated `icecast.xml` (overwritten on restart); changing
  the Caddy config (already returns 404 for `/api/listener-auth`).

## D3 — MCP is control-only

- **Date:** 2026-10-31
- **Status:** decided-by-orchestrator — settled, do not re-litigate
- **Decision:** `/api/mcp` exposes 21 tools: reads, listener requests, and on-air control. There are
  **zero** config writers. Personas, shows, schedule and settings go through the admin HTTP API.
- **Why:** verified with `initialize` + `tools/list` against the live endpoint. The published manual
  contradicts itself (heading 17, table 19, repo doc 20, live 21), so `tools/list` is the only truth.
- **Rejected alternatives:** treating the manual or `docs/mcp-server.md` as authoritative; assuming a
  config surface exists because the tool list is long.

## D2 — Read state files, write the admin API

- **Date:** 2026-10-31
- **Status:** decided-by-orchestrator
- **Decision:** `pull`/`diff` read `settings.json` and `schedule.json` from the root-owned state dir via
  `docker exec`. `apply` writes through the admin HTTP API (`POST /api/settings`, `POST /api/shows`,
  `PUT /api/schedule`).
- **Why:** the read endpoints are presentation views, not the config document — `GET /api/personas`
  returns 4 fields even as admin, `GET /api/schedule` cuts shows to 7 of 26 fields, and admin
  `GET /api/settings` is a 19-key UI summary. The documented OpenAPI export has no config writes at all.
- **Rejected alternatives:** round-tripping config through the documented API (silently truncates
  personas and shows); `GET /api/backup/export` (~99 MB, library included); editing state files directly
  (root-owned, rewritten by the controller).

## D1 — The repo is the source of truth

- **Date:** 2026-10-31
- **Status:** decided-by-orchestrator
- **Decision:** Changes are authored here, reviewed as a diff, then applied with three explicit verbs:
  `pull` (live → repo), `diff` (read-only drift), `apply` (repo → live). Nothing auto-applies: no daemon,
  no cron, no post-commit hook that writes to the station.
- **Why:** the station is live and a bad config change is audible. Review has to happen before the write,
  and drift has to be visible without touching anything.
- **Rejected alternatives:** editing the live instance directly and treating it as authoritative; a
  scheduled auto-sync (drift would go on air unattended).

---

## Open questions (not decided)

These are operator decisions, tracked in `docs/PLAN.md` §12. Do not resolve them in code.

- **Q1 Persona rebalance** — move Sunrise Service to Arthur Dent and Friday Drive to Zaphod Beeblebrox?
- **Q4 Navidrome playlists** — which playlists anchor which shows, and how does the tool learn their ids?
- **Q6 Where the deployment lives** — `deploy/` in this repo, or tracked in `macjuu-private`?
- **Q7 Tool access** — does the sync tool get ssh access to `homeserver`, or does `apply` run on the host?
