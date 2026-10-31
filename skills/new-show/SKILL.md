---
name: new-show
description: >-
  Create a new show (programme) on the SUB/WAVE station, or move an existing one, by editing a
  pulled instance/character/ tree and putting the show into the 7x24 schedule grid, with a
  dry-run and explicit human confirmation before anything is applied. Use when the user says
  "new show", "add a show", "schedule a programme", "make a Sunday morning show", "give
  <persona> a slot", "move <show> to <hours>", or asks which hours a show should own.
---

# New show

A show is a programme: one persona, one brief, genre/mood/era filters, and the hours it owns on the
7×24 grid. Adding one is **two repo edits plus a reviewed apply** — never an API call typed by hand
and never a guess at the show's id. Read [`AGENTS.md`](../../AGENTS.md) for the config API semantics;
this skill is only the order of operations.

## Done when

- `instance/character/shows/<slug>.json` holds a complete show object, with no invented `id`.
- `instance/character/schedule.json` names that slug in the agreed cells, and every changed cell has
  been shown to the human.
- The human has seen the dry-run output and said yes.
- `apply --yes` has run after a reviewed dry run, and `status` / `diff` agree with what was intended.

## Preconditions

1. **Announce the tier.** `python3 scripts/subwave.py status` prints it. Authoring needs a full
   `instance/`, which only a tier-2 pull produces. At tier 1 the reads are trimmed (shows come back
   with 7 of 26 fields), so a lossy capture is **not** an authoring base and must never be applied.
   If there is no full `instance/` and no host access, stop and ask for tier 2 — do not fill the
   missing fields with guesses.
2. **Start from a synced repo.** `python3 scripts/subwave.py diff` must exit 0. If it reports drift,
   pull first (tier 2, plain `pull`, no `--force`) and re-check. Never pull with `--force` after you
   have started editing.
3. **Back up first** if the tier allows it: `python3 scripts/subwave.py backup` (see
   [`backup`](../backup/SKILL.md)). At tier 1 say out loud that no backup is possible.
4. **Check what is on air** and prefer an off-air window. The station is live: the change alters the
   next track and the next talk segment.

## Procedure

### 1. Turn the request into a spec, and confirm it

Collect, asking for anything missing: show name, hosting `<persona>`, the hours (day + hour range,
24h, in the station timezone), and the music brief (genres, moods, eras). Also decide whether the
show uses two hosts (`banter` + `guestPersonaIds`). Do not invent a show that was not asked for.

### 2. Pull before you write

Only if `instance/` is missing (tier 2):

```sh
python3 scripts/subwave.py pull
```

Then locate the hosting persona and the nearest existing show to copy the shape from:

```sh
grep -il "<persona name>" instance/character/personas/*.json
ls instance/character/shows/
```

If the roster has no suitable host, stop: creating a persona is outside this skill.

### 3. Write the show file — no id

Create `instance/character/shows/<slug>.json`, where `<slug>` is a kebab-case version of the name.
Copy a whole existing show and change its fields; do not build a partial object, because the config
API's merges are shallow and a partial object loses fields.

**Never invent `id`.** Ids are server-generated (`s_xxxxxx`). For a new show, leave `id` out; the
server assigns one on create and the next `pull` records the slug→id mapping in
`instance/ids.lock.json`. For an existing show, keep the id that the file already has — that id, not
the slug, is the identity, and changing it creates a duplicate instead of an update.

Minimum fields to get right: `name`, `personaId` (an id that exists in `personas/`), `topic`, the
music filters, `maxTrackSeconds`, and `banter` / `pauseTalk` / `programme`. Start from the field set
in `examples/demo/character/shows/` if there is no local show to copy.

### 4. Edit the grid, then prove which hours changed

`schedule.json` is `{"schedule": {"0".."6": [24 slugs]}, "override": ...}`, where day 0 is Sunday
(JS `getDay()`). Cells hold **slugs**, not ids.

Snapshot before editing, so the change can be shown exactly:

```sh
cp instance/character/schedule.json "${TMPDIR:-/tmp}/schedule.before.json"
```

Edit the agreed cells. Then print every cell that moved:

```sh
python3 - "${TMPDIR:-/tmp}/schedule.before.json" instance/character/schedule.json <<'PY'
import json, sys
before = json.load(open(sys.argv[1])).get("schedule") or {}
after = json.load(open(sys.argv[2])).get("schedule") or {}
days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
for day in sorted(set(before) | set(after)):
    for hour, (a, b) in enumerate(zip(before.get(day, []), after.get(day, []))):
        if a != b:
            print("%s %02d:00  %s -> %s" % (days[int(day)], hour, a, b))
PY
```

**Do not go further until the human has seen this list.** If it shows a cell nobody asked to
change, fix it before proceeding. Keep all 7 days × 24 cells present: the schedule write replaces
the whole grid, so a short day silently wipes hours.

Also check `override`: a one-off override outranks the grid, so a correct edit can look ignored
until it is cleared.

### 5. Dry-run

```sh
python3 scripts/subwave.py apply
```

`apply` dry-runs by default and sends nothing at all; `--dry-run` is accepted for emphasis. Read the
step list it prints. Never hand-craft admin `POST`s from a skill.

### 6. Confirm, then apply

Paste the dry-run summary together with the changed-cell list, and ask for an explicit yes. Only
then run it for real:

```sh
python3 scripts/subwave.py apply --yes
```

The write refuses while a show is on air unless you add `--force-now`, and at tier 1 unless you add
`--no-backup`. Those refusals are the guards working: report them and let the human decide, never
work around them. Announce that the station is live when you do write.

## Verify

```sh
python3 scripts/subwave.py status    # show count +1, grid 168/168 filled, on air
python3 scripts/subwave.py diff      # exit 0: repo and live agree
```

Then confirm the mapping rather than trusting it:

```sh
python3 - instance/character/schedule.json <<'PY'
import json, sys
grid = json.load(open(sys.argv[1])).get("schedule") or {}
slug = "<slug>"
print(sum(1 for day in grid.values() for cell in day if cell == slug), "cells")
PY
```

Finally, listen forward: at the first owned hour, `status` should report the new show on air and the
links should match the brief. A create also earns a row in `docs/applied.md` if the apply step writes
one.

## Failure modes

| Symptom | Cause | Do instead |
| --- | --- | --- |
| Duplicate show, or an existing one silently altered | an invented or reused `id` | omit `id` on create; never retype an existing id |
| Other hours went silent | a partial grid was sent; the write replaces all 168 cells | always edit the pulled file and send the whole grid |
| Show fields missing live | a lossy tier-1 capture was applied (19 of 26 fields dropped) | author only from a full pull; refuse `--allow-lossy` for writes |
| Your edits vanished | `pull --force` ran after editing | pull first, edit second, and never force over uncommitted work |
| 409 on create | the show cap is reached | report the cap; deleting is a human decision, never a sync side effect |
| Show never airs | `personaId` points at no persona, or an `override` is active | validate the id; check `override` |
| Two hosts but one voice | `banter: true` with empty `guestPersonaIds` | set the guest, or turn banter off |
| Change did not take | it was applied mid-programme, or the repo was ahead of live | check `diff`; prefer an off-air boundary |
| Tier 1 asked to do all this | trimmed reads cannot carry a whole show | state the loss and stop |
