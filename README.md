# subwave-marvin

A toolkit for running a **SUB/WAVE** radio station from an agent session: clone it, point an agent at
your station, and ask in plain language — "make a new show", "why does host X talk so much", "make a backup".

Agent-agnostic: the entry point is `AGENTS.md`, so Claude Code, Codex, OpenCode or any other agent picks
up the same instructions; skills and scripts do the mechanical parts.

**Two tiers, announced rather than assumed.** HTTP only: you can create and update config, but reads come
back trimmed and a backup cannot include the schedule. With ssh to the host: full fidelity, including
show definitions and the grid.

```sh
cp .env.example .env     # base URL, admin credentials, station password
# tier 2 only: also set SUBWAVE_HOST and SUBWAVE_STATE_DIR
```

Point your agent here and ask for `scripts/subwave.py status` (the tool lands in P1 — see `docs/PLAN.md`).
`instance/` is gitignored and `examples/demo/` is a fictional station, so the repo is testable without one.
`AGENTS.md` first · `docs/PLAN.md` design · `docs/decision-log.md` why · `docs/applied.md` what went on air
