# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `.env.example` documents `SUBWAVE_CONTAINER`, which the config loader has read and every tier-2
  `docker exec` has used since early on. An install whose controller container is not named
  `sub-wave-controller` could not discover the override, so tier-2 reads failed against a container
  that was not there. A unit test now pins `.env.example` and `Config.KEYS` to the same key set in
  both directions, so they cannot drift again. `ANTHROPIC_API_KEY` is documented as an
  environment-only variable: it is not a `SUBWAVE_*` key and a value in `.env` does nothing.

### Changed

- [`docs/public-readiness.md`](docs/public-readiness.md) is now a record of the completed launch
  rather than a pre-flight checklist: the state at publication, the settings to re-verify, and the
  lesson that a public repository exposes **every branch**, so the leak sweep has to cover remote
  refs and not just `main`.

## [1.0.0] — 2026-10-31

The first public release. A toolkit for running a **SUB/WAVE** radio station from an agent
session: author the station's character, read what is actually live, and push reviewed changes.

### Added

- **Two capability tiers, always announced.** Tier 1 needs only the station's base URL and admin
  credentials; tier 2 adds `ssh` and `docker exec` for full fidelity. Every command prints the
  tier it is running at and what it could not read.
- **`status`** — tier, station, what is on air, and whether the repo matches the live station.
- **`pull`** — captures the live station into `instance/` with every secret replaced by
  `__REDACTED__`, and records `capture.lossy` when a tier-1 read cannot see whole objects.
- **`diff`** — drift between the repo and the live station, by persona, show and grid cell.
- **`validate`** — checks `instance/` (or `--dir` any station) before applying. Unknown fields
  are warnings, missing fields are errors.
- **`apply`** — dry run by default; `--yes` performs the writes. Validates first, refuses a lossy
  capture or a payload still holding `__REDACTED__`, takes a backup, then re-reads the station and
  fails loudly if a field did not land.
- **`apply --prune`** — the only destructive operation, opt-in and dry by default. Refuses to
  delete a show that occupies grid cells or a persona that hosts a show (unless
  `--force-prune`), and hard-refuses the active persona, the last persona and the last show.
- **`backup` / `restore`** — config and assets outside the repo, including `schedule.json` and
  `blocklist.json`, which the station's own export omits. Writes a `RESTORE.md` saying where each
  file goes back.
- **`character`** — scaffold and edit the character layer: `init --from demo`, `new-persona`,
  `new-show`, `add-to-grid`, and `edit-persona` / `edit-show`, which change only the fields you
  name and refuse an unknown field or wrong type rather than coercing it.
- **`blocklist`** — author bans and rules locally with no network, keyed by a slug→uuid lockfile so
  an apply never mints a duplicate rule.
- **`skills`** — author the station's own DJ segments (frontmatter + prompt) without touching the
  image's built-ins.
- **`onair`** — live control over the station's MCP server, kept strictly separate from config.
  Reads are free; every air-changing verb needs `--yes`, and `skip` additionally needs
  `--expect-id` or `--any-track`.
- **Offline test suite** — `tests/mock_station.py` runs a mock controller in-process on loopback,
  so the suite needs no station, no network and no credentials. `examples/demo/` is a committed
  fictional station in the same shape as a real `instance/`.

### Security

- Initial security policy published ([`SECURITY.md`](SECURITY.md)): threat model, controls, and
  plainly stated accepted risks.
- `scripts/check-secrets.sh` blocks live credentials from entering a commit, as a pre-commit hook
  and as a CI job.
- Secret paths are redacted on the way in and named — never printed — by verification.

### Documentation

- [`AGENTS.md`](AGENTS.md) — the operating contract for an agent working the repo.
- [`docs/PLAN.md`](docs/PLAN.md) — design and phases; [`docs/decision-log.md`](docs/decision-log.md)
  — why each choice was made; [`docs/applied.md`](docs/applied.md) — the on-air audit trail.
- [`docs/public-readiness.md`](docs/public-readiness.md) — the checklist for publishing this repo.

[Unreleased]: https://github.com/niels-emmer/subwave-marvin/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/niels-emmer/subwave-marvin/releases/tag/v1.0.0
