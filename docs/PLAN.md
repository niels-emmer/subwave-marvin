# Plan — what is left

This file is a work list, not a design document. How the tool is built lives in
[ARCHITECTURE.md](ARCHITECTURE.md); why each choice was made is in
[decision-log.md](decision-log.md); what shipped is in [CHANGELOG.md](../CHANGELOG.md). When an item here
is finished it moves to one of those, and this file gets shorter.

## Where it stands

Released as v1.0.0 and public. The pipeline is complete and tested: capture, author, validate, diff,
apply with read-back, backup and restore, prune, blocklist authoring, station skills, and on-air control.

One write path has been exercised against a **live** station: `apply` landing `settings` sections (six
house rules), read back and confirmed, `diff` in sync, one row in [applied.md](applied.md). Everything
else is verified against the mock only — which is where the remaining risk is.

---

## 1. Rehearse the five skills end to end

**What.** Nobody has completed a task from a skill alone. The five procedures in `skills/` are merged and
consistent with the tool, but unrehearsed — and an unrehearsed procedure is an unverified one.

**Done when.** An agent, given only [`AGENTS.md`](../AGENTS.md) and `skills/`, completes each of
`add-host`, `new-show`, `explain-host`, `tune-host` and `backup` against `examples/demo/` — at the tier the
skill announces, with the verification step it names — and any place the procedure assumed something the
docs did not say is fixed in the skill.

**Needs.** No live station: the demo branch is enough. A fresh clone, to prove nothing depends on local
state.

## 2. Exercise the write paths that have only ever run against the mock

**What.** These have never touched a live station: show creation (with server-assigned id written back to
the file and the lock), grid replacement (`PUT /api/schedule`, all 168 cells), blocklist rule minting (the
slug ↔ station-uuid lock), station-skill create and update, `restore`, `apply --prune`, and the settings
that only take effect on a **mixer restart** (`privacy.listenerAuth`, `stream.maxListeners`, `opus`, `aac`).

**Done when.** Each has either a row in [applied.md](applied.md) or a documented reason it cannot be
rehearsed safely, and anything a live run exposes is fixed and covered by a test.

**Why it is first-class work.** The first live `apply` found three defects the mock could not have found —
a backup arity crash, the character layer never reaching the station at all, and a read-back that printed
real secrets. Assume a similar class of gap wherever the mock is the only witness.

## 3. Prune does not cover station skills

**What.** `apply --prune` deletes personas, shows and blocklist rules that are on the station but not in
the repo. Station skills are never deleted, so a skill removed from `instance/skills/` stays on the
station indefinitely, and `diff` cannot see the difference.

**Done when.** `--prune` covers skills with the same guards as everything else (dry by default, backup
mandatory, after the write and the read-back, deletion printed individually), or the gap is written into
[AGENTS.md](../AGENTS.md) as intended behaviour rather than as a footnote.

## Open questions — not yet decided

Do not resolve these in code without deciding them here first.

1. **Tier-1 schedule writes.** `PUT /api/schedule` needs all 168 cells and tier 1 can read the grid — but
   tier 1 cannot verify the shows those cells point at. Allow the write, or refuse it?
2. **Playlist anchoring.** Shows can reference Navidrome playlist ids, and nothing in the repo knows how to
   learn them. Navidrome API with the station's own credentials, or a manual list?
3. **Deploy artifacts.** Does the toolkit ship a compose file for a new install, or point at upstream?
4. **Character portability.** Should `examples/demo` double as the starting template users copy into
   `instance/`, or stay purely illustrative?
5. **Skill surface.** Five skills exist. Which tasks deserve the next one — schedule review, restore,
   diagnose a dead stream?
