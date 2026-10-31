---
name: add-host
description: >-
  Create a new SUB/WAVE persona (a DJ host) with a real character brief, give it deliberate dials
  and a voice, and put it on air by assigning it to a show, with a dry-run and explicit human
  confirmation before anything is applied. Use when the user says "add a host", "new DJ", "new
  persona", "hire someone", "create a presenter", "add another voice to the station", or asks to
  put a new host on a particular show or slot.
---

# Add a host

A persona is not a name in a list — its `soul` field **is the system prompt** the station sends to
the model before every talk segment. So this skill has one rule that outranks the rest: **no
character brief, no persona.** `character new-persona` scaffolds a soul that is a TODO skeleton, and
that skeleton is shape-valid: `validate` passes it with zero warnings and `apply` would happily put a
host with no character on air. Nothing downstream will catch it. The brief is the only gate.

Read [`AGENTS.md`](../../AGENTS.md) for where persona config lives (inside `settings.json` on the
host, one file per persona in the repo) and how a write is applied. This skill is only the order of
operations.

## When to use

- "Add a host", "new DJ", "new persona", "hire someone", "create a presenter".
- "Give me a second voice for the late show", "I want a host for the Sunday slot".
- A show exists but has no one to front it.

Not for changing an existing host — that is [`tune-host`](../tune-host/SKILL.md), and for working out
*why* a host sounds the way it does, [`explain-host`](../explain-host/SKILL.md). Not for scheduling
an existing host's programme: that is [`new-show`](../new-show/SKILL.md). A host with no show is
invisible on air, so this skill always finishes by assigning one.

## Done when

- The human has supplied a character brief, and `instance/character/personas/<slug>.json` holds a
  soul written from it — no `TODO` anywhere in the file.
- Every dial was chosen deliberately and can be justified in one sentence each.
- A voice is set, or the human has been told explicitly that the station's default will speak.
- The persona is the `personaId` of a show that owns grid hours, and the changed cells have been
  shown to the human.
- The human has seen the dry-run output and said yes; `apply --yes` has run after it.
- `status` and `diff` agree with what was intended.

## Preconditions

1. **Announce the tier.** `python3 scripts/subwave.py status` prints it. Authoring needs a full
   `instance/`, which only a tier-2 pull produces; at tier 1 the persona read is trimmed to 14 of 18
   fields, so a lossy capture is not an authoring base. If there is no full `instance/` and no host
   access, stop and ask for tier 2 — do not fill the missing fields with guesses.
2. **Start from a synced repo.** `python3 scripts/subwave.py diff` must exit 0. If it reports drift,
   pull first (tier 2, plain `pull`, no `--force`) and re-check.
3. **Back up first** if the tier allows it: `python3 scripts/subwave.py backup` (see
   [`backup`](../backup/SKILL.md)). At tier 1 say out loud that no backup is possible.
4. **Check what is on air.** The station is live: a new host changes the next talk segment of any
   show it fronts. Prefer an off-air window, and never write mid-programme without saying so.

## Procedure

### 1. Get the character brief — before you create anything

Ask, and wait for answers. A brief that can be turned into a system prompt has four parts:

- **Identity** — who they are in two or three sentences: their job, their temperament, what they
  sound like at this hour.
- **Voice** — how they talk: sentence length, what they notice, what they never do.
- **Catchphrases** — one or two lines they could actually say. Optional, but a host with none is
  usually a host with no character.
- **Place** — which show and hours they front, and whether they share it with a second host.

If the human gives only a name ("add a host called <persona>"), say plainly that a name is not a
host and that shipping the scaffold would put a DJ with no character on air. Offer to draft the soul
from three or four answers, then have them approve the draft. Do not invent a personality and
present it as done.

### 2. Scaffold the file

```sh
python3 scripts/subwave.py character new-persona <slug> --name "<Name>"
```

That writes all 18 fields, with `frequency: moderate`, `scriptLength: concise`, `djMode: true`,
`linkStyle: natural`, the three integer dials at 5, and a placeholder id `p_local_<slug>`. Persona
ids are preserved by the station (personas travel inside the settings array), so the placeholder
becomes the real id — unlike a show's, which is dropped and reassigned. Re-running this for an
existing slug is refused, which is the guard against a rename creating a duplicate.

### 3. Fill the soul

The scaffolded soul is three TODO headings. Replace all of it with the brief, written **as
instructions to the model, in the second person** — that is what it is:

```sh
python3 scripts/subwave.py character edit-persona <persona> --set soul="$(cat "${TMPDIR:-/tmp}/soul.txt")"
```

`--set soul=` takes the text verbatim, newlines included, so write the prose to a file first rather
than fighting shell quoting. Keep the scaffold's shape — `IDENTITY`, `VOICE`, `CATCHPHRASES` — because
it is what the rest of the roster looks like.

Then read it back and check no `TODO` survives:

```sh
grep -c TODO instance/character/personas/<persona>.json
```

`grep` must print `0` (it exits 1 when there is nothing to find — that is the pass, not an error).
This is not optional politeness: `validate` does not look inside `soul`, so a leftover skeleton
passes every automated check in the toolkit.

### 4. Choose the dials deliberately

Defaults are a starting point, not a decision. Each of these is set the same way:
`character edit-persona <persona> --set FIELD=VALUE`.

| Field | Vocabulary | Choose it because |
| --- | --- | --- |
| `frequency` | `silent`, `quiet`, `moderate`, `chatty`, `aggressive` | How often the host speaks between records. It is an ordered ladder, so pick the rung, not a mood. `silent` means it never speaks. |
| `scriptLength` | `one-liner`, `concise`, `extended`, `storyteller` | How long each link runs. Also a ladder, shortest to longest. |
| `djMode` | `true` / `false` | Gates back-announce and transition talk — the links over the seams between records. It is **not** the silence switch; the DJ still speaks on the `frequency` dial. Leave it `true` unless the brief asks for no back-announce. |
| `linkStyle` | `natural`, `announce` | The register of a link: conversational hand-off, or presenter announcement. |
| `humour` | integer | Higher is more jokes. |
| `localColour` | integer | Higher means more local references and detail. |
| `warmth` | integer | Higher is warmer; lower is drier and more clipped. |

Write down one sentence per dial — "chatty because the brief says it talks after most records" —
and show the set to the human. A host at `storyteller`/`aggressive` when the brief asked for a quiet
overnight voice is a mistake the brief would have caught.

**Vocabulary refusals are guards.** The ladders above are the station's. The toolkit carries its own
copy of that list, and a copy older than the station will refuse a rung the station accepts (a value
like `quiet` or `extended`). Report the refusal and stop; do not hand-edit the JSON to slip a value
past the check, and do not substitute a neighbouring value without telling the human.

### 5. Pick a voice

Voice lives in two fields, both on the persona:

- `tts` — a JSON object: `{"engine": "kokoro", "cloudProvider": "", "voice": "", "gainDb": 0, "speed": 1}`
  in this repo's shape. `voice` is an **engine-specific voice id**.
- `voiceStyle` — free text describing delivery; the toolkit passes it through unchanged.

```sh
python3 scripts/subwave.py character edit-persona <persona> \
  --set 'tts={"engine":"kokoro","cloudProvider":"","voice":"<voice id>","gainDb":0,"speed":1}'
```

**Never invent a voice id.** Copy one from an existing persona whose sound the human likes, or read
the station's TTS settings. If no id is known, leave `voice` empty and tell the human that the
engine's default voice will speak — that is a real outcome they should hear about before it airs, not
a detail.

Also worth setting from the brief, same command: `tagline` (one line, shown with the host), `musicLean`
(list of leanings), `tags`, `language`, `avatar`.

### 6. Put the host on a show

A persona that hosts nothing never speaks. Two paths:

**New show for this host** — `--persona` takes a **slug** and writes the persona's id for you:

```sh
python3 scripts/subwave.py character new-show <show-slug> --name "<Show name>" --persona <persona>
python3 scripts/subwave.py character add-to-grid <show-slug> --days fri --from 22 --to 24
```

`add-to-grid` refuses to take cells that already hold another show until you pass `--replace`; read
what it prints and get the human's agreement before replacing someone else's hours. Follow
[`new-show`](../new-show/SKILL.md) for the brief, the music filters and the grid review discipline.

**Existing show, new host** — `--set personaId=` takes a **literal value and does not resolve a
slug**. Read the id from the persona file first:

```sh
python3 -c "import json;print(json.load(open('instance/character/personas/<persona>.json'))['id'])"
python3 scripts/subwave.py character edit-show <show-slug> --set personaId=<id>
```

Passing the slug here writes a slug into `personaId`, which `validate` does not cross-check — the
show would point at nothing and the station would have no host for those hours. If the show uses two
hosts, set `banter=true` and put the second persona's id in `guestPersonaIds`; banter with no guest
is one voice pretending to be two.

### 7. Validate

```sh
python3 scripts/subwave.py validate
```

Errors block the write. Warnings do not, so read them rather than skimming the exit code.

### 8. Dry-run

```sh
python3 scripts/subwave.py apply
```

`apply` dry-runs by default and sends nothing; `--dry-run` is accepted for emphasis. Check that the
persona appears in the `POST /api/settings` step by name, and that every show and grid step you
intended is listed. Never hand-craft admin `POST`s from a skill.

### 9. Confirm, then apply

Show the human: the soul, the dials with their one-line justifications, the voice, the show and
cells the host now fronts, and the dry-run summary. Ask for an explicit yes. Only then:

```sh
python3 scripts/subwave.py apply --yes
```

**A refusal here is the guard working, not a bug to route around.** `apply` refuses a lossy capture,
a payload still holding `__REDACTED__`, a write while a show is on air without `--force-now`, and a
tier-1 write without `--no-backup`. Report the exact refusal to the human and let them decide. Never
work around one.

## Verify

```sh
python3 scripts/subwave.py status    # persona count +1, on air state, grid filled
python3 scripts/subwave.py diff      # exit 0: repo and live agree
```

Then confirm the two things that actually make a host audible:

```sh
python3 - <<'PY'
import glob, json
personas = {p["id"]: p for p in (json.load(open(f)) for f in glob.glob("instance/character/personas/*.json"))}
for f in sorted(glob.glob("instance/character/shows/*.json")):
    show = json.load(open(f))
    if show.get("personaId") in personas:
        p = personas[show["personaId"]]
        print("%-14s hosts %-14s frequency=%s scriptLength=%s djMode=%s soul_todo=%s"
              % (f.split("/")[-1][:-5], p["name"], p["frequency"], p["scriptLength"],
                 p["djMode"], "TODO" in p.get("soul", "")))
PY
```

`hosts` must name the show you intended, and `soul_todo` must be `False` for every host you touched.
Then listen forward: at the first owned hour, `status` should report that show on air, and the links
should sound like the brief rather than like a generic announcer.

## Failure modes

| Symptom | Cause | Do instead |
| --- | --- | --- |
| A host on air with no character | the scaffolded TODO soul was shipped; `validate` cannot see inside `soul` | get the brief first, write the soul, `grep -c TODO` must be 0 |
| Persona exists, nothing changed on air | it hosts no show, or the show owns no grid hours | assign it in step 6 and check the cells |
| Show airs with no host, or the wrong one | `personaId` was set to a slug, or to another persona's id | read the id from the persona file; `validate` does not cross-check it |
| Two hosts, one voice | `banter: true` with empty `guestPersonaIds` | set the guest id, or turn banter off |
| "persona already exists" | re-running `new-persona` for a slug already on disk | edit the existing persona with `tune-host`; never create a duplicate to rename |
| Refused: enum outside the allowed set | the toolkit's vocabulary copy is older than the station's | report it; do not hand-edit the file or pick a neighbouring rung silently |
| Refused: lossy capture | a tier-1 `--allow-lossy` pull is the base | re-pull with host access; a trimmed persona loses 4 fields |
| Refused: still holds `__REDACTED__` | a secret was never rehydrated | fix the secret at tier 2; never write the placeholder over a live credential |
| Refused: mid-show | the station is on air | wait for the boundary, or pass `--force-now` only with explicit human consent |
| Refused: tier 1 without a backup | no host access to take one | report it; the human decides, and `--no-backup` is their call, not yours |
| Other hours changed | `add-to-grid --replace` took cells that held another show | print the changed cells and get agreement before replacing |
| Edits vanished | `pull --force` ran after editing | pull first, edit second, never force over uncommitted work |
| Host sounds nothing like the brief | dials set from habit, or a soul that contradicts them | re-read the soul as a prompt; `tune-host` fixes dials, and the soul wins over them |
