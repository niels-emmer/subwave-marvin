---
name: explain-host
description: >-
  Diagnose why a SUB/WAVE persona talks as much, as little, or in the tone it does, by reading its
  dials and soul alongside the shows it hosts, then proposing the smallest config change that
  would fix it. Use when the user says "why does <persona> talk so much", "too chatty", "too
  quiet", "the host never stops talking", "make the DJ say less", "persona tuning", or asks what a
  dial like frequency, scriptLength, humour or banter actually does.
---

# Explain a host

This is a **diagnosis** skill. It reads, explains and proposes; it does not write. A station that
talks too much is almost never fixed by rewriting the persona's `soul` — it is fixed by finding the
one dial that is actually causing it. Read [`AGENTS.md`](../../AGENTS.md) for where persona config
lives (inside `settings.json` on the host, split into files in the repo) and how a write is applied.

## When to use

- "Why does <persona> talk so much / so little?"
- "Too chatty", "too quiet", "the links are too long".
- "What does `scriptLength` / `banter` / `pauseTalk` do?"
- Someone wants persona tuning and has not decided what to change.

Not for creating or scheduling a show ([`new-show`](../new-show/SKILL.md)), and not for backups
([`backup`](../backup/SKILL.md)). If the human approves a change, this skill stops and the apply
workflow (planned) takes over.

## Preconditions

1. `python3 scripts/subwave.py status` — announce tier, what is on air, and whether the repo has
   drifted. If `diff` is dirty, the dials you are about to read are not what is playing: pull first
   (tier 2) or say that the diagnosis is against the repo, not the station.
2. A full-fidelity `instance/`. At tier 1 `/api/schedule` trims shows to 7 of 26 fields and there is
   no `GET /shows`, so **`banter`, `pauseTalk`, `segmentSkill` and `preparationSkill` are invisible**.
   At that tier you may explain the persona dials, but you must refuse to name a show-level cause.
3. Know which persona. If the user gave a show instead, resolve it to its `personaId` first.
4. Safe on a live station: everything below is read-only.

## How the dials work

Persona file — `instance/character/personas/<persona>.json`:

| Field | What it controls | Effect on "too much talking" |
| --- | --- | --- |
| `frequency` | **how often** the DJ speaks between tracks. Values seen in the shape include `moderate` and `chatty`. | The primary volume dial. A chatty setting means a link after most records. |
| `scriptLength` | **how long** each link runs. `storyteller` is long-form, `concise` short. | Long links make the same number of links feel relentless. |
| `djMode` | Whether the persona talks at all. `false` is a silent host. | Only a cause when it is on and the user expected silence. |
| `linkStyle` | The register and hand-off of a link (e.g. `natural`). | Shape, not volume. Tune last. |
| `humour` | Integer tone dial; higher is more jokes. | Perceived chatter, not frequency. |
| `localColour` | Integer; higher means more local references and detail. | Adds words per link. |
| `warmth` | Integer; higher is warmer, lower is drier and clipped. | Low warmth reads as curt, not quiet. |
| `soul` | **The system prompt itself.** Free prose. | Prose can demand stories and length that outrun every dial — check it before blaming dials. |
| `houseRules` | In `character.json`, station-wide. | A rule like "never rush" or "keep talk short" can override a tuned dial. |

Show file — `instance/character/shows/<slug>.json`:

| Field | What it controls | Effect |
| --- | --- | --- |
| `banter` | Two-host conversation with `guestPersonaIds`. | **Doubles the voices and multiplies the talk. If it is on, that is usually the answer.** |
| `pauseTalk` | Whether music pauses/ducks under talk. | Same words feel much longer when the record stops for them. |
| `programme` | Structured programme mode rather than plain links. | Adds structure — and often segments. |
| `segmentSkill` | A skill that generates segments. | Non-empty means talk **beyond** the between-track links. |
| `preparationSkill` | A preparation step that feeds the links. | Richer material, usually longer links. |

## Procedure

### 1. Get the whole picture in one read

```sh
python3 - <<'PY'
import glob, json, os

root = os.environ.get("SUBWAVE_CHARACTER", "instance/character")
personas = [json.load(open(p)) for p in glob.glob(root + "/personas/*.json")]
shows = {os.path.basename(p)[:-5]: json.load(open(p))
         for p in glob.glob(root + "/shows/*.json")}
grid = (json.load(open(root + "/schedule.json")).get("schedule") or {})
days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

def hours_of(slug, show):
    aliases = {slug, show.get("id")}
    return sorted((int(d), h) for d, cells in grid.items()
                  for h, cell in enumerate(cells) if cell in aliases)

def ranges(hours):
    out = []
    for day, hour in hours:
        if out and out[-1][0] == day and out[-1][2] == hour - 1:
            out[-1] = (day, out[-1][1], hour)
        else:
            out.append((day, hour, hour))
    return ", ".join("%s %02d-%02d" % (days[d], a, b + 1) for d, a, b in out) or "no hours"

for p in sorted(personas, key=lambda x: x.get("name") or ""):
    print("%s (%s)" % (p.get("name"), p.get("id")))
    print("  frequency=%s scriptLength=%s djMode=%s linkStyle=%s humour=%s localColour=%s warmth=%s"
          % tuple(p.get(k) for k in ("frequency", "scriptLength", "djMode",
                                      "linkStyle", "humour", "localColour", "warmth")))
    hosted = [(s, v) for s, v in shows.items() if v.get("personaId") == p.get("id")]
    for slug, show in sorted(hosted):
        print("  show %-14s %s" % (slug, ranges(hours_of(slug, show))))
        print("    banter=%s guests=%s pauseTalk=%s programme=%s segmentSkill=%r preparationSkill=%r"
              % (show.get("banter"), show.get("guestPersonaIds"), show.get("pauseTalk"),
                 show.get("programme"), show.get("segmentSkill"), show.get("preparationSkill")))
    if not hosted:
        print("  hosts no show")
PY
```

### 2. Read the soul, not just the dials

Open the persona's `soul` in full and read it as the system prompt it is. Look for instructions that
manufacture talk: "tell stories", "never rush", "fill the silence", "one idea per link" (a brake), or
a fixed sign-off every time.

### 3. Read the shows it hosts

A persona is shared. If it hosts three shows and only one feels chatty, the cause is in that show's
overrides, not in the persona. Note the hours each show owns — a host with 18 hours a day *feels*
omnipresent even at a moderate frequency.

### 4. Rank the causes, in this order

1. **`banter: true` with guests** — two voices; usually the answer.
2. `djMode: true` + a chatty `frequency`.
3. A long `scriptLength` (e.g. `storyteller`).
4. `programme: true`, or a non-empty `segmentSkill` / `preparationSkill`.
5. `soul` prose that asks for length or stories, or a `houseRules` entry that does.
6. Exposure: many hours, or an `override` putting the host somewhere unexpected.
7. `linkStyle`.

Rule out each one with evidence before moving to the next. If the show has banter on, say so first
and stop looking.

### 5. State the diagnosis, then one minimal change

Write it as: the exact field, the exact current value, the file it lives in, and why it produces the
behaviour. Then propose **one** field change with before → after, what the listener will notice, and
what will not change.

Examples of a minimal proposal (do not apply them yourself):

- `banter: true` → `false` on that one show, leaving `guestPersonaIds` intact to restore later.
- `frequency: chatty` → `moderate` on the persona — note this changes **every** show it hosts.
- `scriptLength: storyteller` → `concise`, if links are long but not frequent.

Prefer the show-level override when only one show is wrong; prefer the persona dial when all of them
are. If two changes are wanted, apply them one at a time so the effect is attributable.

### 6. Hand off

Give the human the exact JSON edit and ask. Applying is the write workflow's job (`apply` is
planned) — this skill never writes, so nothing here can change what is on air.

## Verify (after someone applies it)

1. `python3 scripts/subwave.py diff` — clean, so the change is live and not just in the repo.
2. `python3 scripts/subwave.py status` — the same show is on air, the persona unchanged elsewhere.
3. Listen across two or three track boundaries in the affected show, and one boundary in an
   unaffected show hosted by the same persona, to confirm the change is scoped as claimed.
4. If `frequency` changed, check a show the persona hosts **outside** the reported one: the change
   should be visible there too, which is the point to confirm with the human.

## Failure modes

| Symptom | Cause | Do instead |
| --- | --- | --- |
| Confident answer that does not match what airs | diagnosed from a drifted repo, or from an `override` | check `diff` and `override` first |
| "It is the persona" but only one show is chatty | show overrides were never read | read every hosted show before ranking |
| Guessed at tier 1 | `banter`/`pauseTalk`/segment fields are trimmed away | explain the dials, refuse the show-level verdict |
| Rewrote the soul and nothing improved | the cause was a dial, or the change was never applied | change one field; verify with `diff` |
| Changed one show, expected all | persona edits are global, show edits are local | say which scope the proposal has |
| `banter: true` blamed, but no guests | banter cannot run without `guestPersonaIds` | look at `frequency` and `scriptLength` |
| Fixed the talk but the station feels worse | `pauseTalk` was the real complaint (music stopping) | separate "more words" from "music stops" |
| Proposal applied mid-programme | live station | prefer an off-air boundary and say so |
