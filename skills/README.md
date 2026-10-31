# Skills

A skill here is one directory, `skills/<name>/SKILL.md`: a YAML header with a `name` and a
`description`, then a procedure an agent can follow without re-deriving the station's rules. The
`description` is the matching surface — it says what the skill does **and** the sentences that
should trigger it. The body is procedure, not doctrine: `AGENTS.md` is the authority on tiers, the
config API and safety, and every skill links back to it rather than restating it.

These are agent-agnostic in format and in content: a YAML header with a `description` is the shape every
agent matches on, and nothing here names a station, host, persona or show — the operator's own data stays
in gitignored `instance/`, referred to as `<slug>` and `<persona>`.

**The path is not one any agent scans automatically.** Claude Code reads `.claude/skills/`, OpenCode reads
`.opencode/skills/`, and Codex has no skills directory at all. So discovery runs through `AGENTS.md`: its
"Which procedure to load" table is the trigger surface, and it points here. Add a skill there, or it is
findable only by an agent that happens to list this directory.

## The five

`add-host`, `new-show`, `explain-host`, `tune-host` and `backup`. The trigger sentences — and which verb
decides when a request fits two of them — live in [`AGENTS.md`](../AGENTS.md) under **Which procedure to
load**. Keep them there: a second copy here is a second thing to fall out of date.

## Why diagnosis and change are separate skills

`explain-host` only reads; `tune-host` writes. A host that talks too much has one change at the end of it,
and running the diagnosis first is what stops a station getting two changes at once and no idea which one
helped. `AGENTS.md` carries the routing rule and the tier announcement every skill owes the operator.

## Writing another

Copy the section order: when to use, done-when, preconditions (tier, backup), numbered procedure
with the exact commands, verification, failure modes. Keep it verifiable: if a flag is not in
`scripts/subwave.py --help` or `AGENTS.md`, it does not go in the skill — say it is planned.
