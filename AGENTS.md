# AGENTS.md — subwave-marvin

Config-as-code for **Marv's Tunes**, a private, Hitchhiker's-Guide-themed SUB/WAVE radio station at
https://radio.macjuu.com. This repo is the source of truth for personas, shows, the weekly schedule and
station settings; `tools/subwave.py` (planned) pulls live state in and pushes reviewed changes out.
Design, phases and open questions: `docs/PLAN.md`.

## Mental model

It is a radio station, not a playlist. One Icecast stream; every listener hears the same track at the same
moment; there is no skip and no per-listener queue. A **show** is a programme — persona + free-text brief +
genre/mood/era filters — and the **7×24 grid** decides which show owns each wall-clock hour. A **persona** is
an LLM DJ that talks between tracks; its `soul` field *is* the system prompt. Everything runs live, so a config
change alters the next track and the next talk segment, not some offline artifact.

## Live system (verified 2026-10-31)

- ssh alias `homeserver` (192.168.101.252, user `zaph0d`). Containers: `sub-wave-{web,controller,broadcast,caddy,analyzer,docker-proxy}` plus `navidrome`.
- Compose dir `/home/zaph0d/macjuu/stacks/subwave` (`compose.yaml` + `.env`) is **untracked** in `macjuu-private`, so the deployment is not reproducible from git.
- State dir `/home/zaph0d/dockerdata/subwave/state` → `/var/sub-wave`. Files are **root-owned**: plain `ssh`/`scp` as zaph0d gets `Permission denied`.
- Caddy publishes host port **7700** (API + web). The controller's 7701 is container-internal; nothing listens on host 7701.

### Read live config — the only correct way

```sh
ssh homeserver 'docker exec sub-wave-controller cat /var/sub-wave/settings.json'
ssh homeserver 'docker exec sub-wave-controller cat /var/sub-wave/schedule.json'
ssh homeserver 'docker exec sub-wave-controller ls /var/sub-wave'
```

Never `scp`, `chown`, or hand-edit those files: the controller owns them and rewrites them.

- `settings.json` (~20 KB, 52 top-level keys) is the whole station config; `personas[]` and `activePersonaId` live **inside it**, so persona and station config are one document.
- `schedule.json` = `{shows[], schedule, override}`; `schedule` maps day `"0".."6"` (JS `getDay()`, 0 = Sunday) to 24 show ids.
- `skills/<slug>/SKILL.md` holds custom skills: 12 on the host, 5 enabled.
- Runtime or derived, never copy or edit: `library.db*`, `secrets.env`, `icecast-secrets.env`, `icecast_listener_auth.txt`, `liquidsoap_*.txt` (mirrors of settings knobs), `audience.json`, `queue.json`, `recent-plays.json`, `archive/`, `sessions/`, `logs/`.

## The API is not the documented API

`GET /api/connect/openapi.json` is a hand-curated 33-path subset (requires auth) with **no config writes**; the read endpoints are presentation views, not the config document:

- `/api/personas` → only `id`, `name`, `tagline`, `avatar` — even with admin auth.
- `/api/schedule` → the grid plus shows reduced to 7 of their 26 fields.
- `/api/settings` (admin) → a **19-key UI summary** (`onAir`, `pickerBusy`, `defaults`, …), *not* `settings.json`.

So: **reads come from the state files above; writes go to the admin API.** Real write routes, from upstream `controller/src/routes/`, absent from the OpenAPI export:

| Route | Semantics |
| --- | --- |
| `POST /api/settings` | admin, **shallow partial merge**. Send whole top-level sections only: `{"personas":[…]}` replaces the entire array, and a partial persona silently loses fields. |
| `POST /api/shows` | admin, body `{"show":{…}}`, **upsert by `show.id`**; no id creates a new show (409 once the show cap is hit). Returns `{shows, show}`. |
| `PUT /api/schedule` | admin, **replaces the whole grid** — always send all 7×24 cells. |
| `POST` / `DELETE /api/schedule/override` | admin, single programme override. |
| `POST /api/personas/import`, `GET /api/personas/:id/export`, `*/personas/:id/avatar`, `DELETE /api/personas/:id`, `DELETE /api/shows/:id` | admin; the deletes are destructive. |
| `GET /api/backup/export` | admin, ~99 MB gzip backup — not a config-sync path. |

Auth classes: public (none), station (`x-station-auth` listener password; gates `/api/similar-tracks`), admin
(HTTP Basic `ADMIN_USER`/`ADMIN_PASS`).

**MCP is control-only, and that is settled.** `/api/mcp` exposes **21 tools** live (verified via `tools/list`;
`similar_tracks` and `queue_block` are newer than the published docs): health, now_playing, station_state,
schedule, session, request_song, request_status, search_library, similar_tracks, queue_track, queue_block,
skip_track, dj_announce, dj_segment, list_skills, run_skill, list_sfx, play_sfx, list_jingles, play_jingle,
refresh_playlist. **Zero config tools — no persona, show, schedule or settings writer exists over MCP**; those go
through the admin HTTP API. Do not re-litigate this from `/manual/mcp`, which contradicts itself (heading 17,
table 19, repo doc 20, live 21): `tools/list` is the truth. Ids are server-generated: `p_xxxxxx` personas,
`s_xxxxxx` shows.

## Safety

- The station is **live**. Never apply config during a show you would not want to hear break: check
  `/api/now-playing` first, prefer an off-air window, and warn listeners with a DJ segment when in doubt.
- Never commit secrets. `ADMIN_PASS`, the station password (`settings.privacy.password`), `ANTHROPIC_API_KEY`,
  and the keys inside `settings.json` (`llm.apiKey`, `llm.fallback.apiKey`, `tts.cloud.*`, `search.apiKey`,
  `embedding.apiKey`, `scrobble.*`) come from a gitignored `.env` or the macOS Keychain at apply time. `pull`
  redacts them; `apply` must never transmit the redaction sentinel.
- Deletes (`DELETE /api/shows/:id`, `DELETE /api/personas/:id`) run only when the human asks, never as a side
  effect of a sync.
- Do not start a second SUB/WAVE stack against the same state dir.

## Commands

Live read and verify — every one of these was run against the live station:

```sh
ssh homeserver 'curl -s http://localhost:7700/api/health'          # {"status":"on-air"}
ssh homeserver 'curl -s http://localhost:7700/api/now-playing'
ssh homeserver 'curl -s http://localhost:7700/api/schedule' | python3 -m json.tool | head
# admin calls: keep the credentials on the host, never on your own command line
ssh homeserver 'cd /home/zaph0d/macjuu/stacks/subwave && set -a; . ./.env; set +a; \
  curl -s -u "$ADMIN_USER:$ADMIN_PASS" http://localhost:7700/api/settings'
```

Planned — **not implemented yet**, see `docs/PLAN.md`:

```sh
tools/subwave.py pull                # live state -> repo, secrets redacted
tools/subwave.py diff                # read-only drift report
tools/subwave.py apply --dry-run     # print payloads, send nothing
tools/subwave.py verify              # repo grid == /api/schedule
python3 -m unittest discover -s tests
```

## Layout

`config/` is the source of truth: `settings.json` (secrets redacted), `personas/<slug>.json`,
`shows/<slug>.json`, `schedule.json` (grid by slug), `skills/<slug>/SKILL.md`, and `ids.lock.json` — a
generated slug→server-id map, never hand-edited, that is what keeps a rename from creating a duplicate.
`tools/` is Python 3.14 stdlib only. `tests/fixtures/` holds redacted recorded snapshots. Read `docs/PLAN.md`
before editing anything under `config/`.
