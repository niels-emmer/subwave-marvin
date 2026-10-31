---
name: tune-host
description: >-
  Change a SUB/WAVE persona's dials — or one show's talk settings — to fix a specific complaint,
  one field at a time, with validate, a dry-run and explicit human confirmation before anything is
  applied. Use when the user says "make <persona> warmer", "tune the host", "tune <persona>", "less
  chatty", "shorter links", "fewer jokes", "more local", "change the dials", or names a host and a
  behaviour they want different.
---

# Tune a host

[`explain-host`](../explain-host/SKILL.md) diagnoses; this skill **changes**. It carries out the one
minimal change that diagnosis named. The discipline that makes it safe is the same one that makes the
result attributable: **read the persona and every show it hosts before touching a dial, change one
thing, and treat the `soul` as the last resort rather than the first.**

Read [`AGENTS.md`](../../AGENTS.md) for where persona config lives and how a write is applied. This
skill is the order of operations, not the doctrine.

## When to use

- "Make <persona> warmer", "make <persona> less chatty", "shorter links", "fewer jokes".
- "Tune the host", "tune <persona>", "change the dials", "turn the talking down".
- A diagnosis has been made — by `explain-host` or by the human — and someone now has to write it.

If the complaint is still unexplained ("why does it talk so much?"), stop and load
[`explain-host`](../explain-host/SKILL.md) first: tuning before diagnosing is how a station gets two
changes at once and no idea which one helped. Not for creating a host
([`add-host`](../add-host/SKILL.md)), and not for moving a show's hours
([`new-show`](../new-show/SKILL.md)).

## Done when

- The complaint has been mapped to **one** named field, and the human has agreed that is the target.
- The scope is stated out loud: persona edits change **every** show that host fronts; show edits
  change one show.
- Exactly one field has changed per apply, with before → after shown to the human.
- `validate` is clean, the human has seen the dry-run, and `apply --yes` has run after it.
- `diff` is clean afterwards, and the change was listened for in the affected show and, if the
  persona dial moved, in one other show the same host fronts.

## Preconditions

1. **Announce the tier.** `python3 scripts/subwave.py status` prints it, along with what is on air
   and whether the repo has drifted. A drifted repo means the dials you are about to edit are not
   what is playing: pull first (tier 2) or say the change is against the repo, not the station.
2. **A full-fidelity `instance/`.** At tier 1 `/api/schedule` trims shows to 7 of 26 fields, so
   `banter`, `pauseTalk`, `segmentSkill` and `preparationSkill` are invisible. At that tier you may
   tune persona dials, but you must refuse to name or edit a show-level cause.
3. **Back up first** if the tier allows: `python3 scripts/subwave.py backup` (see
   [`backup`](../backup/SKILL.md)). At tier 1 say out loud that no backup is possible.
4. **Know which persona.** If the human named a show, resolve it to its `personaId` first.
5. **Check what is on air.** The change alters the next talk segment of every show the host fronts;
   prefer an off-air boundary.

## Procedure

### 1. Read the persona and its shows first

Never tune from the complaint alone. The same persona can sound right on one show and relentless on
another, and only the shows tell you why.

```sh
python3 - <<'PY'
import glob, json
personas = {p["id"]: (p, f) for f in glob.glob("instance/character/personas/*.json")
            for p in [json.load(open(f))]}
for pid, (p, path) in sorted(personas.items()):
    if p.get("name") != "<persona>":
        continue
    print("%s  (%s)" % (path, pid))
    print("  frequency=%s scriptLength=%s djMode=%s linkStyle=%s humour=%s localColour=%s warmth=%s"
          % tuple(p.get(k) for k in ("frequency", "scriptLength", "djMode", "linkStyle",
                                      "humour", "localColour", "warmth")))
    for sf in sorted(glob.glob("instance/character/shows/*.json")):
        show = json.load(open(sf))
        if show.get("personaId") == pid:
            print("  show %-16s banter=%s guests=%s pauseTalk=%s programme=%s segmentSkill=%r"
                  % (sf.split("/")[-1][:-5], show.get("banter"), show.get("guestPersonaIds"),
                     show.get("pauseTalk"), show.get("programme"), show.get("segmentSkill")))
PY
```

For the full picture — hours owned, souls, `houseRules`, exposure — use the read in
[`explain-host`](../explain-host/SKILL.md) step 1. Also read the `soul` in full, and the station's
`houseRules` in `instance/character/character.json`: a rule like "never rush" outranks a tuned dial.

### 2. Map the complaint to the dial that causes it

| The complaint | The dial | Notes |
| --- | --- | --- |
| "Talks too much" / "too chatty" | `frequency` | The primary volume dial. Ladder: `silent`, `quiet`, `moderate`, `chatty`, `aggressive`. |
| "Links are too long" | `scriptLength` | Ladder: `one-liner`, `concise`, `extended`, `storyteller`. |
| "Too many jokes" / "trying too hard" | `humour` | Integer; lower is drier. |
| "Too dry", "curt", "clipped" | `warmth` | Integer; higher is warmer. This is tone, not volume — it will not make a host talk less. |
| "Too many local references" | `localColour` | Integer; lower means fewer asides and details. |
| "Sounds like an announcer" / "too formal" | `linkStyle` | `natural` (conversational hand-off) or `announce` (presenter announcement). |
| "Never speaks" | `djMode`, then `frequency` | `djMode: false` is a silent host; `frequency: silent` is the same outcome on the ladder. |
| "Two voices, twice the talk" | `banter` on the **show** | With `guestPersonaIds`; usually the real answer when it is on. |
| "The music keeps stopping" | `pauseTalk` on the **show** | Same words feel much longer when the record ducks out for them. |
| "A whole segment I did not ask for" | `programme`, `segmentSkill` on the **show** | Structured mode adds talk beyond the between-track links. |

If two rows fit, the first one that matches wins — `banter` before `frequency`, `frequency` before
`scriptLength`. Say which one you ruled out and why, in one line each.

### 3. Decide the scope, and say it out loud

- The complaint is **one show** → change that show (`edit-show`). Local, reversible, attributable.
- The complaint is **the host everywhere** → change the persona (`edit-persona`). Global: every show
  it fronts changes at once.

Prefer the show when only one show is wrong. Say the scope before editing, because "I changed the
persona" is a much bigger sentence than it sounds.

### 4. Change one thing

Persona dial:

```sh
python3 scripts/subwave.py character edit-persona <persona> --set warmth=7
```

Show setting:

```sh
python3 scripts/subwave.py character edit-show <show> --set pauseTalk=false
```

`--set` is repeatable, but **run one field per apply** so the effect is attributable — unless the
human explicitly asks for a bundled change, in which case say so and expect not to know which part
worked. `edit-persona` and `edit-show` change only the fields you name, leave every other field's
value and position alone, and never touch an `id` or a slug. They refuse an unknown field (listing
the real ones) and a wrong type or enum rather than coercing it.

**Vocabulary refusals are guards.** The ladders above are the station's. The toolkit carries its own
copy of that list, and a copy older than the station will refuse a rung the station accepts (a value
like `quiet` or `extended`). Report the refusal and stop; do not hand-edit the JSON to slip a value
past the check, and do not silently substitute a neighbouring value.

### 5. Only now consider the soul

The `soul` **is the system prompt**, and it can override every dial. If the prose asks for stories,
length, or "fill the silence", a `frequency` change will not save you — the model is being told to
talk.

That makes a soul rewrite the **last resort, not the first**:

- It changes everything about the host at once, so nothing is attributable afterwards.
- It cannot be justified by a complaint about one dial.
- It needs a fresh brief from the human, not an improvised edit.

So: change the dial, apply, listen. Rewrite the soul only if the dial change did not fix it and the
soul is demonstrably asking for the behaviour. Then show the human the new prose before it is
written, exactly as [`add-host`](../add-host/SKILL.md) step 3 requires.

### 6. Validate

```sh
python3 scripts/subwave.py validate
```

### 7. Dry-run

```sh
python3 scripts/subwave.py apply
```

`apply` dry-runs by default and sends nothing; `--dry-run` is accepted for emphasis. Confirm the step
list names the entity you meant to change — a persona edit shows the persona by name in the
`POST /api/settings` step, a show edit shows that show.

### 8. Confirm, then apply

Show the human: the field, the before → after, the scope (which shows it touches), what the listener
will notice, and what will **not** change. Ask for an explicit yes. Only then:

```sh
python3 scripts/subwave.py apply --yes
```

**A refusal here is the guard working, not a bug to route around.** `apply` refuses a lossy capture,
a payload still holding `__REDACTED__`, a write while a show is on air without `--force-now`, and a
tier-1 write without `--no-backup`. Report the exact refusal and let the human decide.

## Verify

```sh
python3 scripts/subwave.py diff      # exit 0: the change is live, not just in the repo
python3 scripts/subwave.py status    # same show on air, persona unchanged elsewhere
```

Then listen, deliberately:

1. Two or three track boundaries in the affected show — the complaint should be gone.
2. If a **persona** dial moved, one boundary in another show the same host fronts. The change should
   be visible there too; that is the scope you promised, so confirm it rather than assume it.
3. If `pauseTalk` moved, check whether the music now runs under the talk instead of stopping.

A change you cannot hear at a boundary has not taken: check `diff`, then check whether an `override`
is in force, before changing anything else.

## Failure modes

| Symptom | Cause | Do instead |
| --- | --- | --- |
| Changed a dial, nothing sounds different | the soul (or a `houseRules` entry) is asking for the behaviour | read the soul before blaming the dial; rewrite it only as a last resort |
| The whole station changed when one show was wrong | a persona edit, which is global | prefer `edit-show` when only one show is at fault; say the scope first |
| Two changes, no idea which helped | `--set` was repeated in one apply | one field per apply |
| "Too chatty" fixed by warmth, still chatty | `warmth` is tone, not volume | `frequency`, then `scriptLength` |
| Host now sounds curt instead of quiet | `warmth` lowered when the ask was fewer links | separate "fewer words" from "colder words" |
| Refused: enum outside the allowed set | the toolkit's vocabulary copy is older than the station's | report it; do not hand-edit or pick a neighbouring rung silently |
| Refused: lossy capture | a tier-1 `--allow-lossy` pull is the base | re-pull with host access; a trimmed show hides the talk fields |
| Refused: still holds `__REDACTED__` | a secret was never rehydrated | fix the secret at tier 2; never write the placeholder over a live credential |
| Refused: mid-show | the station is on air | wait for the boundary, or `--force-now` only with explicit human consent |
| Refused: tier 1 without a backup | no host access to take one | report it; `--no-backup` is the human's call |
| Refused: tier 1 and a show-level cause | `banter`/`pauseTalk` are trimmed away | explain the dials, refuse the show-level edit, ask for tier 2 |
| Change looked applied but air is unchanged | an `override` outranks the grid, or the repo was ahead of live | check `diff` and `override` |
| Soul rewritten and it got worse | the cause was a dial, and the rewrite removed a brake | revert to the dial change; a rewrite needs a new brief |
