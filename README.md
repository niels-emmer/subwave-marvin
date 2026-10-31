# subwave-marvin

Configuration-as-code for **Marv's Tunes**, a private SUB/WAVE radio station themed around
The Hitchhiker's Guide to the Galaxy.

Personas, shows, the weekly schedule and station settings are defined here, and a small sync tool
(planned, see the plan) pushes them to the live instance so a change is reviewable before it goes on air.

- Agents: read `AGENTS.md` first.
- Plan and phases: `docs/PLAN.md`.
- Decisions: `docs/decision-log.md`. Applied changes: `docs/applied.md`.

Before committing, run `sh scripts/check-secrets.sh` — the pre-commit hook runs it for you once
`git config core.hooksPath .githooks` has been set.
