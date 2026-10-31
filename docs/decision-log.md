# Decision log

Append-only, newest first. Each entry: **Date / Decision / Why / Rejected alternatives.**
Never rewrite an entry; supersede it with a new one and mark the old one `superseded by Dn`.

---

## D12 — The container name is configurable, and `.env.example` is kept in parity by a test

- **Date:** 2026-10-31
- **Status:** decided (supersedes the "Container name" open question; also supersedes the "Do not resolve
  these in code" instruction for that one question only)
- **Decision:** `SUBWAVE_CONTAINER` is a supported config key, documented in `.env.example`, defaulting to
  the image default `sub-wave-controller`. It is what every tier-2 `docker exec` uses. A unit test asserts
  that the keys in `.env.example` and `Config.KEYS` are exactly the same set, in both directions, so the
  two cannot drift again. `ANTHROPIC_API_KEY` is documented as a comment only: it is read from the real
  environment (used to rehydrate a redacted `llm.apiKey` on apply) and is deliberately **not** a
  `SUBWAVE_*` key, because `load_dotenv` reads only `Config.KEYS` and a value in `.env` would silently do
  nothing.
- **Why:** the code had already resolved the open question — the key was in `Config.KEYS` and wired
  through every `docker exec` — while `.env.example` omitted it, so an operator whose container has
  another name could not discover the override and tier-2 reads just failed against a container that was
  not there. The gap was invisible because nothing compared the two lists. The decision log said "do not
  resolve in code"; the honest fix is to finish the decision and record it, not to leave the code and the
  docs disagreeing.
- **Rejected alternatives:** documenting the assumption instead and removing the key (it is already
  implemented and tested, and a renamed container is install-specific, so pretending it is fixed would
  break real installs); leaving the key undocumented (undiscoverable is the same as unsupported for an
  operator); adding `ANTHROPIC_API_KEY` to `Config.KEYS` (the loader would then read it from `.env`,
  quietly widening where a credential may live).

## D11 — Edit vocabularies are sourced from upstream, with a forced-value escape

- **Date:** 2026-10-31
- **Status:** decided-by-orchestrator (supersedes the "Vocabularies" paragraph of D10 and its claim
  that `moods` is an enum; D10's edit semantics otherwise stand)
- **Decision:** the edit-time enum sets are copied from upstream SUB/WAVE, not inferred from this repo's
  sample data. Persona: `controller/src/schemas/persona.ts` `PERSONA_FREQUENCIES` (silent, quiet,
  moderate, chatty, aggressive), `PERSONA_SCRIPT_LENGTHS` (one-liner, concise, extended, storyteller),
  `PERSONA_LINK_STYLES` (natural, announce), re-exported by `controller/src/settings/vocab.ts` as
  `FREQUENCIES` / `SCRIPT_LENGTHS` / `LINK_STYLES`. Show: `controller/src/schemas/show.ts`
  `SHOW_ENERGY` (low, medium, high) and `SHOW_VOCALS` (instrumental, vocal; the field also accepts `''`).
  Lists keep the product's ladder order because `controller/src/settings/persona.ts` bumps a persona one
  rung with `FREQUENCIES.indexOf(...)`. `moods` is **not** an enum upstream — `showStringList` validates
  it against the station's own installed `ctx.moodNames` — so it stays a free-form list here. A value
  outside a known set is still refused, and `--force-value` allows an unrecognised value with a loud
  warning, for a station newer than this toolkit.
- **Why:** D10's sets were read off `examples/demo` and the fixture, and were incomplete: they refused
  `frequency=aggressive` and `scriptLength=extended`, both valid live values. A hard rejection on a
  guessed list turns the tool into the thing that is wrong about a correct edit.
- **Rejected alternatives:** widening the lists by guesswork (the same defect, just later); dropping
  rejection entirely (a typo would reach the station); validating `moods` against a fixed list (the mood
  names are install-specific, so a fixed list would block valid moods).

## D10 — Editing is a field-level set verb, not a whole-object rewrite

- **Date:** 2026-10-31
- **Status:** decided-by-orchestrator
- **Decision:** `character edit-persona <slug> --set FIELD=VALUE` and `character edit-show <slug>
  --set FIELD=VALUE` (repeatable `--set`) change only the named fields of an existing entity. Every
  unnamed field keeps its value **and its JSON position**; `id` is immutable and a rename never changes
  the slug, the file name or the lockfile. Values are typed at edit time: integers reject non-integers,
  booleans accept only `true`/`false`, and enums (`frequency`, `scriptLength`, `linkStyle`, a show's
  `moods`/`energies`) reject anything outside the vocabulary with the allowed set in the message. An
  unknown field is refused with the list of real fields. The command writes the file, re-runs
  `validate_instance()`, and restores the original text if the result would fail, so an edit cannot
  leave an invalid entity behind. It is pure local file editing: no network, no tier.
- **Why:** the common ask is "make host X warmer" or "turn banter on", and a hand-edit is exactly what
  the station's shallow settings merge punishes — a dropped field disappears on apply. A targeted verb
  keeps the edit reviewable as a one-line diff and makes the typo/type failure loud at edit time instead
  of silent at write time. Separate verbs (rather than one generic `set`) keep the per-entity field
  whitelist obvious and mirror `new-persona`/`new-show`.
- **Vocabularies:** the station's full enum is not published in this repo, so the allowed sets are the
  values its own shape uses — scaffold defaults, `examples/demo`, `tests/fixtures/station.json` and the
  values named in `skills/explain-host/SKILL.md`. A value outside them is refused rather than guessed
  at. `validate` itself stays permissive about values (unknown fields are warnings), because a newer
  server may know more than this repo does; only the edit verbs are strict.
- **Rejected alternatives:** read-modify-write through the API (tier 1 reads are trimmed, so it would
  delete unseen fields); a whole-object `--replace-file` flow (same failure mode, and it makes a
  one-dial change unreviewable); accepting unknown fields as a warning (a typo would be ignored and the
  edit would silently do nothing); stringifying integers and guessing booleans from `1`/`yes` (the
  station's merge does not tell you it ignored the value).

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

- **Tier-1 schedule writes** — allow `PUT /api/schedule` at tier 1, or refuse because shows cannot be read?
- **Playlist anchoring** — how does the toolkit learn Navidrome playlist ids?
- **Deploy artifacts** — ship a compose file, or point at upstream?
- **Demo as template** — is `examples/demo` illustrative only, or the thing users copy?
- **Skill surface** — which tasks get a skill first?
