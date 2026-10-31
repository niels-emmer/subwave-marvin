# PLAN — subwave-marvin

Implementation plan for making the **Marv's Tunes** SUB/WAVE station programmable from git: personas, station
settings, shows and the weekly schedule defined in this repo, pulled from and pushed to the live instance.

Status: plan only. Nothing here is implemented yet; no live config has been modified.

---

## 1. Verified current state (2026-10-31)

Read from `/var/sub-wave/settings.json` and `/var/sub-wave/schedule.json` via
`ssh homeserver 'docker exec sub-wave-controller cat …'`.

| Area | Live value |
| --- | --- |
| Station | "Marv's Tunes"; description "Brain the size of a Universe, here I am picking songs for meatbags."; `timezone` Europe/Amsterdam; `locale` en-GB; `theme.active` classic-dark |
| LLM | `anthropic` / `claude-haiku-4-5`; trackSelection `agentic`; requestMatching `agentic`; noRepeatWindow 250; artistVarietyWindow 5; `dailyTokenCap` 0 |
| TTS | enabled; kokoro; per-persona voices `bm_lewis` / `bm_daniel` / `bm_george` |
| Privacy | `privatePlayer` true, **`listenerAuth` false**, `publishPersonaSouls` false, `password` set (23 chars) |
| Stream | **`maxListeners` 100**, bitrate 192, flac true, bufferSeconds 22, idleWhenEmpty true, opus/aac disabled |
| Requests | enabled; maxPending 6; perIpHourlyCap 8; globalHourlyCap 30; cooldownSec 60; repeatCooldownMin 120; onePendingPerIp true |
| Skills | 12 `SKILL.md` on host; enabled: commute-check-in, dystopian-update, good-news, news-briefing, philocrisis |
| Personas | 4 — Marvin `p_389727`, Zaphod Beeblebrox `p_9de2cf`, Arthur Dent `p_f01623`, Ford Prefect `p_7c487e`; souls 1.2–1.3 KB |
| Shows | 6 — Thank You-Know-Who It's Friday `s_747743`, Friday Drive `s_d33a46`, Sunrise Service `s_560bdd`, Late Feels `s_01ee08`, Working Day Vibes `s_82943d`, Weekend Daylight Blend `s_e43bd6` |
| DJ prompts | `djPrompt`, `djPrompts`, `djHouseRules` all **empty** |
| Grid | Sun/Sat: Sunrise 00–06, Weekend Daylight 07–22, Late Feels 23; Mon–Thu: Sunrise 00–06, Working Day 07–22, Late Feels 23; Fri: Thank You-Know-Who 00–17, Friday Drive 18–23 |
| Hosting | Marvin hosts 5 of 6 shows; Ford hosts Weekend Daylight Blend |

## 2. The constraint that shapes the design

The documented API is not the config API. Verified:

- `GET /api/personas` returns 4 fields per persona (`id, name, tagline, avatar`) — even with admin auth.
- `GET /api/schedule` returns the grid plus shows cut down to 7 of their 26 fields.
- `GET /api/settings` (admin) returns a 19-key UI summary (`onAir`, `pickerBusy`, `defaults`, …) — **not** `settings.json`.
- `GET /api/connect/openapi.json` is a curated 33-path subset with no config writes.
- `GET /api/backup/export` is a ~99 MB gzip blob (library included) — unusable for config sync.

**Therefore the tool has two asymmetric halves: reads from root-owned state files via `docker exec`; writes via
the admin HTTP API.** Any design that tries to round-trip config through the read endpoints will silently
truncate personas and shows. This is the single most important thing in this plan.

## 3. Assumptions (explicit)

1. **The repo is the source of truth.** The live station is a deployment target, not the authority. Changes are
   authored in git, reviewed, then applied.
2. Three verbs, no magic: `pull` (live → repo), `diff` (read-only drift), `apply` (repo → live). **Never
   auto-apply.** No cron, no daemon, no post-commit hook that writes to the station.
3. `apply` is idempotent: applying twice changes nothing the second time.
4. **Secrets never enter the repo.** `pull` redacts on the way in; `apply` re-injects live values at the moment
   of writing (secret-preserving merge, §5.4).
5. **Python 3.14 stdlib only** (`urllib`, `json`, `subprocess`, `unittest`). Python 3.14.6 is available. No
   `jsonschema`, no `requests`, no third-party deps — the validator is ~150 lines of explicit code (§6).
   Node 26 / Bun 1.4 are available but are not used: one language, zero install step.
6. **A slug→server-id lockfile is mandatory.** Server ids are generated (`p_xxxxxx`, `s_xxxxxx`); the lockfile is
   what makes renames safe and prevents duplicate personas/shows.
7. **Publishing: local commits only.** No push, no PR for this work.
8. The human runs `apply`. The agent may prepare, dry-run, and report.

## 4. Repo layout

```
AGENTS.md
docs/PLAN.md
docs/apply-log/                 # one JSON per applied change (hashes, no secrets)
config/
  settings.json                 # whole settings doc, secrets replaced by "__REDACTED__"
  personas/<slug>.json          # one complete persona object per file
  shows/<slug>.json             # one complete show object per file
  schedule.json                 # {"grid": {"0": ["<slug>", …24]}, "override": null}
  skills/<slug>/SKILL.md        # captured from the host
  schema/fields.json            # field spec: types, enums, ranges, required sets
  schema/vocab.json             # captured vocabularies: moods, energies, vocals, linkStyle…
  ids.lock.json                 # slug -> server id, generated
deploy/
  compose.yaml                  # from /home/zaph0d/macjuu/stacks/subwave
  env.example                   # keys only, values blank
  README.md                     # how to bring the stack up on a fresh host
tools/
  subwave.py                    # CLI entry point
  sw_state.py                   # state-dir reads over ssh/docker exec
  sw_api.py                     # admin API client (urllib, Basic auth)
  sw_config.py                  # repo <-> live model: slugs, ids, redaction, merge
  schema.py                     # validator
tests/
  fixtures/2026-10-31/          # redacted snapshot: settings, schedule
  test_pull.py test_schema.py test_apply_payloads.py test_redaction.py
```

Why one file per persona/show: it makes diffs reviewable ("what changed about Arthur?") and rename-safe (the
filename is the slug, the `id` inside is the link). Why the grid stores slugs and not ids: a grid of `s_xxxxxx`
is unreadable in review, and ids change on a rebuild of the station.

## 5. Sync tool design

### 5.1 Connection model

```sh
# reads: root-owned files, via the controller container
ssh homeserver 'docker exec sub-wave-controller cat /var/sub-wave/settings.json'

# writes: admin API through a tunnel; 7701 is container-internal, so tunnel 7700
ssh -N -L 17700:localhost:7700 homeserver &
tools/subwave.py apply --dry-run --base-url http://127.0.0.1:17700
```

Credentials come from the environment, never argv (argv is world-readable in `ps`):

```sh
export SUBWAVE_ADMIN_USER=...   SUBWAVE_ADMIN_PASS=...
# or, on macOS, straight from the Keychain:
export SUBWAVE_ADMIN_PASS="$(security find-generic-password -s subwave-admin -w)"
```

### 5.2 `pull` — live → repo

1. Read `settings.json`, `schedule.json`, and every `skills/*/SKILL.md` via `docker exec`.
2. Redact secret paths (§5.5) to `"__REDACTED__"`.
3. Split: `personas[]` → `config/personas/<slug>.json` (slug from `name`); `shows[]` → `config/shows/<slug>.json`;
   everything else → `config/settings.json`; grid ids → slugs via the lockfile.
4. Regenerate `ids.lock.json`.
5. Refuse to write if any live secret value survives redaction (a substring check of the output against the live
   secret values) — a redaction bug must fail loudly, not leak.

`pull --check` writes nothing and exits non-zero when the repo already matches.

### 5.3 `diff` — read-only drift report

Compares repo-desired against live-actual, per section and per entity, and prints a unified diff of the JSON.
Ignores redacted paths and a small allowlist of server-normalized fields (populated during Phase 1 by observing
what a no-op round trip changes). `--exit-code` for scripting. `diff` never needs credentials for the read half
and never writes.

### 5.4 `apply` — repo → live

Order matters, because shows reference personas and the grid references shows:

1. **Preflight.** `GET /api/health`; `GET /api/now-playing`; print current show and listener count. Refuse when
   `--require-off-air` and the station is on air. Print a summary of every pending change and require `--yes`.
2. **Read live** `settings.json` + `schedule.json` (same path as `pull`).
3. **Build desired** documents, resolving slugs → ids through `ids.lock.json`.
4. **Secret-preserving merge.** For each top-level section that contains a redacted path, take the live value at
   that path and the repo value everywhere else. This is what lets us flip `privacy.listenerAuth` without
   clobbering `privacy.password` under a shallow merge.
5. **Write, one section at a time**, verifying after each:
   | Step | Call | Why this order |
   | --- | --- | --- |
   | 1 | `POST /api/settings` `{"personas":[…], "activePersonaId":…}` | shows point at persona ids |
   | 2 | `POST /api/shows` `{"show":{…}}` per changed show | upsert by `show.id` from the lockfile |
   | 3 | `POST /api/settings` one section per call (`stream`, `privacy`, `requests`, `djHouseRules`, `skills`, …) | smallest possible blast radius per call |
   | 4 | `PUT /api/schedule` with all 7×24 cells | whole-grid replace; do it last |
6. **Confirm.** Re-read state and diff against the intended result; abort on mismatch and report which step.
7. **Audit.** Write `docs/apply-log/<UTC timestamp>.json`: sections touched, before/after SHA-256 of the redacted
   documents, ids created. No secrets, no souls, no listener data.

Never in scope for `apply`: deletes. `DELETE /api/shows/:id` and `DELETE /api/personas/:id` are separate,
explicit, human-only verbs (`subwave.py delete-show <slug> --yes`). `--prune` is deliberately not implemented in
v1.

### 5.5 Redaction and secrets

Secret paths inside `settings.json` (all confirmed present):

```
privacy.password
llm.apiKey                     llm.fallback.apiKey
tts.cloud.apiKey               tts.cloud.compatApiKey
search.apiKey                  embedding.apiKey
scrobble.lastfm.apiKey         scrobble.lastfm.apiSecret    scrobble.lastfm.sessionKey
scrobble.listenbrainz.userToken
```

Rules:

- Redaction is a path allowlist (`config/schema/fields.json` → `"secret": true`), not a regex over values.
- The sentinel is `"__REDACTED__"`. `apply` **must** refuse to send any payload containing it (§6 validation).
- `pull` fails closed: if a live secret value appears anywhere in the produced files, abort without writing.
- `.env` is gitignored; `deploy/env.example` carries key names with empty values.
- `ANTHROPIC_API_KEY`, `ADMIN_PASS` and the station password live in the host `.env` / macOS Keychain, never here.

### 5.6 `ids.lock.json` — the rename guard

```json
{ "version": 1,
  "personas": { "marvin": "p_389727", "zaphod-beeblebrox": "p_9de2cf" },
  "shows":    { "sunrise-service": "s_560bdd", "friday-drive": "s_d33a46" } }
```

Generated by `pull`, never hand-edited, and checked in. Invariants enforced by the validator and by `apply`:

- slug ↔ id is a bijection (no two slugs share an id, no id appears twice).
- Renaming a file/`name` keeps the id → `POST /api/shows` upserts instead of creating a duplicate.
- A slug in `config/` with no id, while a live entity of the same name exists, is an error that demands
  `--adopt <slug>=<id>` — never a silent create.
- An id in the lockfile that the live station no longer has is reported, not auto-deleted.
- A live persona/show with no file in `config/` is drift, reported by `diff`, never pruned automatically.

### 5.7 CLI surface

```
subwave.py pull [--check] [--out DIR]
subwave.py diff [--exit-code]
subwave.py validate [--strict]
subwave.py apply [--dry-run] [--yes] [--only settings,personas,shows,schedule] [--require-off-air]
subwave.py verify
subwave.py ids
```

`--dry-run` prints the exact JSON bodies and the ordered call list, and sends nothing.

## 6. Schema and validation

No third-party validator. `tools/schema.py` reads `config/schema/fields.json` and enforces, per entity:

- **Persona (18 fields):** `id, name, tagline, frequency, scriptLength, djMode, linkStyle, humour, localColour,
  warmth, soul, musicLean, language, voiceStyle, avatar, tts{engine,cloudProvider,voice,gainDb,speed}, skills[],
  tags[]`. Required: all of them — a partial persona is data loss, because `POST /api/settings` replaces the whole
  `personas` array. Enums: `frequency` (moderate|aggressive|chatty|…), `scriptLength` (concise|extended|…),
  `linkStyle` (storyteller|…), captured from live into `vocab.json`. Ranges: `humour`/`warmth`/`localColour` 0–10.
  `soul` length 200–6000 chars and must contain an `IDENTITY` section.
- **Show (26 fields):** `id, name, topic, personaId, guestPersonaIds[], banter, pauseTalk, programme,
  segmentSkill, preparationSkill, moods[], themeId, genres[], energies[], eras[{fromYear,toYear}], vocals,
  filtersStrict, maxTrackSeconds, minTrackLengthSeconds, fadeAtShowEnd, playlistIds[], playlistStrict,
  playlistExhaust, playlistInOrder, excludedPlaylistIds[], tags[]`.
- **Grid:** exactly 7 day keys `"0".."6"`, exactly 24 cells each, every cell resolving to a slug in the lockfile.
- **Cross-references:** `show.personaId` and `guestPersonaIds` exist; `activePersonaId` exists; referenced
  `skills` exist under `config/skills/`; `themeId` exists (or is the active theme).
- **No sentinel leakage:** `"__REDACTED__"` is legal only in `config/settings.json`, never in an apply payload.
- **Unknown fields are an error** (`--strict` default). A field the server has that we do not model means our
  model would delete it on merge; failing loudly is correct.

## 7. Phases

### Phase 0 — Bootstrap the baseline (read-only)

Capture what exists today, before proposing any change, so drift is provable.

- `pull` the live state into `config/` (redacted), capture all 12 `SKILL.md`, copy `compose.yaml` + `.env` **key
  names** into `deploy/`, and record `tests/fixtures/2026-10-31/`.
- Add `.gitignore` (`.env`, `*.log`, `docs/apply-log/*.tmp`).
- Commit: "chore: capture live Marv's Tunes config as baseline".

**Exit criteria:** `subwave.py validate` passes; `subwave.py diff` reports zero drift; `grep -r` for the live
secret values over `config/` finds nothing.

### Phase 1 — Read-only tooling

`sw_state.py` (ssh + docker exec), `pull`, `diff`, `validate`, `verify`, and the validator. Round-trip study:
pull → diff → pull again; record which fields the server normalizes into a small allowlist.

**Exit criteria:** `pull --check` is clean after a fresh pull; golden tests green against the recorded fixtures;
`verify` passes against the live station without any credentials beyond the tunnel.

### Phase 2 — Author the intended config (no writes)

Encode §8's changes as reviewable diffs in `config/`. Every change is a normal git diff plus a dry-run payload
dump.

**Exit criteria:** `validate` passes; `apply --dry-run` prints payloads that a human has read line by line;
`diff` against live shows exactly the intended changes and nothing else.

### Phase 3 — First real apply (writes, off-air)

Smallest first: `djHouseRules` alone (`POST /api/settings` with one section). Then personas. Then shows. Then the
grid. One section per apply invocation, each with its own `apply-log` entry.

**Exit criteria:** `verify` passes; `pull --check` reports no drift; `/api/schedule` grid matches the repo grid;
`/api/now-playing` still advances; a listener can still play the stream.

### Phase 4 — Deployment reproducibility

`deploy/` becomes a working bring-up: `compose.yaml`, `env.example`, a README with the exact `docker compose up`
sequence and the state-dir bind mount. Decide whether `macjuu-private` keeps the canonical copy (§12 Q6).

**Exit criteria:** a fresh checkout can describe the running stack completely; the only missing pieces are the
two secrets.

### Phase 5 — Hardening (optional)

Scheduled `diff` in CI-style cron that only reports; per-section `apply`; theme/sfx/jingle metadata capture
(assets stay out of git).

## 8. Proposed config changes

Each item is a hypothesis to review, not a decision already taken.

| # | Gap | Proposal | Risk |
| --- | --- | --- | --- |
| 1 | `listenerAuth` false — the web player is password-gated but `/stream.mp3` is open to anyone with the URL | set `privacy.listenerAuth: true`; confirm the Icecast-side auth (`icecast_listener_auth.txt` exists in the state dir) is wired | medium: a wrong flip locks out invitees; test with one listener before announcing |
| 2 | `maxListeners` 100 vs "never more than a handful" | `stream.maxListeners: 8` | low |
| 3 | Marvin hosts 5 of 6 shows | Sunrise Service → Arthur Dent (reluctant early riser); Friday Drive → Zaphod Beeblebrox (party, "always on"); keep Thank You-Know-Who, Working Day Vibes and Late Feels with Marvin; Weekend Daylight stays Ford → Marvin 3 / Ford 1 / Zaphod 1 / Arthur 1 | low: `personaId` swap only, guest lists already carry the other voices |
| 4 | `playlistIds` empty on all 6 shows — no Navidrome anchoring | one curated Navidrome playlist per show; `playlistStrict` on for genre shows, off for banter shows | medium: needs Navidrome playlist ids (§12 Q4); wrong anchoring starves the picker |
| 5 | `djHouseRules`, `djPrompt`, `djPrompts` empty | add global house rules: British English; never break character or mention being an AI; no real-world politics, medical, legal or financial advice; never spoil the Guide's answer; never read back listener data; keep talk under ~45 s; never announce the same track twice; no station-format talk during instrumental-only shows | low, and it is the cheapest visible quality win — Phase 3 step 1 |
| 6 | `stacks/subwave` untracked in `macjuu-private` | `deploy/` in this repo: `compose.yaml` + `env.example` + README | low |
| 7 | 12 `SKILL.md` only on the host | `config/skills/<slug>/SKILL.md` in git, plus the enabled map in `settings.skills.enabled` | low |
| 8 | `dailyTokenCap` 0 (unlimited) | discuss; suggest a cap once the house rules are stable | low cost risk |

Not proposed: changing the model (Haiku is right for a 24/7 station), changing bitrate, adding shows beyond the 6
(the show cap is server-enforced).

## 9. Verification story

| # | Check | Command | Needs |
| --- | --- | --- | --- |
| V1 | Model is valid and self-consistent | `tools/subwave.py validate` | nothing |
| V2 | Idempotent pull | `tools/subwave.py pull --check` | ssh |
| V3 | No drift | `tools/subwave.py diff --exit-code` | ssh |
| V4 | Payloads are what we think | `tools/subwave.py apply --dry-run` + golden snapshot test | nothing |
| V5 | Grid landed | `tools/subwave.py verify` (compares repo grid and show ids against `GET /api/schedule`) | tunnel |
| V6 | No secret escaped | `tests/test_redaction.py`; `git grep -c __REDACTED__` matches the expected count | nothing |
| V7 | Fixtures round-trip | `pull` on `tests/fixtures/2026-10-31` reproduces the same files | nothing |
| V8 | Station still works | `curl -s .../api/health`, `/api/now-playing` advances, stream plays | tunnel |

`verify` deliberately uses the **public** `/api/schedule` endpoint (grid + show ids only) so post-apply checking
does not need root access. The deep check is V3/V2 over the state files.

Golden tests run with `python3 -m unittest discover -s tests`, no network.

## 10. Rollout order

1. **Read-only everything** (Phase 0–1): pull, diff, validate, verify. Zero write risk.
2. **Dry-run** (Phase 2): payloads reviewed by a human.
3. **One section, off-air** (Phase 3): `djHouseRules`.
4. **Personas**, then **shows**, then the **grid**.
5. **Deletes**: only ever manually, one at a time, with a backup export first.

Before the first real write: take a `GET /api/backup/export` (or a copy of `settings.json` + `schedule.json` via
`docker exec`) and keep it outside the repo.

## 11. Secrets handling

- In repo: never. `config/settings.json` carries `"__REDACTED__"` at the eleven paths in §5.5.
- At runtime: `SUBWAVE_ADMIN_USER` / `SUBWAVE_ADMIN_PASS` from a gitignored `.env` or the Keychain
  (`security find-generic-password -s subwave-admin -w`).
- On the host: `/home/zaph0d/macjuu/stacks/subwave/.env` keeps `ADMIN_USER, ADMIN_PASS, SITE_URL, TZ, STATE_DIR,
  ANTHROPIC_API_KEY`; `deploy/env.example` mirrors the key names with empty values.
- The station/listener password is `settings.privacy.password` — it lives in the state file and in the Keychain,
  never in git.
- `apply` refuses payloads containing the sentinel; `pull` aborts if a live secret value appears in its output.

## 12. Open questions for the human

1. **Persona rebalance (§8.3)** — approve Sunrise → Arthur and Friday Drive → Zaphod? It is a `personaId` swap
   with no data loss, but it changes who you hear when.
2. **Listener auth** — is `/stream.mp3` actually open today, and does flipping `listenerAuth` depend on Icecast
   config outside `settings.json`? What is the acceptable downtime to find out?
3. **`maxListeners` 8** — right number, or do you want headroom (e.g. 12)?
4. **Navidrome playlists** — which playlists anchor which shows, and how should the tool learn playlist ids
   (Navidrome API with the credentials in `settings.navidrome`, or by hand)? Until answered, `playlistIds` stays
   empty and that gap stays open.
5. **`dailyTokenCap` 0** — set a cap? What monthly spend is acceptable for a 24/7 station?
6. **Where does the deployment live** — `deploy/` in this repo (my recommendation, it is this instance's repo) or
   tracked inside `macjuu-private`? Duplicating it in both is the one option I would avoid.
7. **Does the tool get ssh access to `homeserver`**, or should `apply` run on the host instead? Today the read
   half requires `docker exec` as root.
8. **Renaming** — confirm the convention: the filename is the slug, `id` inside the file is the link, and a rename
   is a `git mv` that must keep the `id`.
9. **Grid semantics** — the grid is local wall-clock hours in Europe/Amsterdam. Confirm it stays local time across
   DST (i.e. a 07:00 show is 07:00 in both CET and CEST).
10. **Empty cells** — may a grid cell be unassigned, or must all 168 always resolve to a show? (Needs a source
    read to confirm the schema; the current grid has no gaps.)
11. **Assets** — themes, sfx, jingles, beds, voices and avatars are binary and stay out of git. Confirm that
    metadata-only capture is enough.

## 13. Non-goals

Not a music library or a Navidrome replacement. No binary assets in git. No on-air control (that is the MCP
server's job). No deploy automation, no auto-apply, no daemon, no pruning of live entities.
