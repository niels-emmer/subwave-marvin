# Skills

A skill here is one directory, `skills/<name>/SKILL.md`: a YAML header with a `name` and a
`description`, then a procedure an agent can follow without re-deriving the station's rules. The
`description` is the matching surface — it says what the skill does **and** the sentences that
should trigger it. The body is procedure, not doctrine: `AGENTS.md` is the authority on tiers, the
config API and safety, and every skill links back to it rather than restating it.

These are agent-agnostic. Claude Code, Codex and OpenCode all discover them the same way: read the
descriptions, match the user's sentence, load the body. Nothing here names a station, host, persona
or show — the operator's own data stays in gitignored `instance/`, referred to as `<slug>` and
`<persona>`.

## The three

| Skill | Answers | Say something like |
| --- | --- | --- |
| [`new-show`](new-show/SKILL.md) | create a programme and put it on the grid | "new show", "add a show", "schedule a programme", "give <persona> a Sunday slot" |
| [`explain-host`](explain-host/SKILL.md) | diagnose why a persona talks the way it does | "why does <persona> talk so much", "too chatty", "persona tuning", "make the DJ say less" |
| [`backup`](backup/SKILL.md) | take a config backup that can actually be restored | "backup", "back up the station", "export the config", "save the config" |

## How an agent picks one

1. Match the verb in the user's sentence against the descriptions. **Create/place** something →
   `new-show`. **Explain/diagnose/tune** an existing host → `explain-host`. **Copy/preserve**
   state → `backup`.
2. If two seem to fit, the verb decides: "why does the new show's host talk so much" is
   `explain-host` (diagnosis) and only becomes `new-show` if the user then asks to change the show.
3. If none fit, do not improvise a procedure: read `AGENTS.md`, run
   `python3 scripts/subwave.py status`, and ask. `apply` dry-runs by default and needs `--yes` to
   write; its guards, not the skill, decide whether a write may proceed.
4. Whichever skill loads, announce the tier it is running at and whether the station is on air
   before doing anything else.

## Writing another

Copy the section order: when to use, done-when, preconditions (tier, backup), numbered procedure
with the exact commands, verification, failure modes. Keep it verifiable: if a flag is not in
`scripts/subwave.py --help` or `AGENTS.md`, it does not go in the skill — say it is planned.
