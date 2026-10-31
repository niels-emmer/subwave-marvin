---
name: backup
description: >-
  Take a restorable backup of a SUB/WAVE station's configuration — settings.json plus the
  schedule.json that the built-in API export omits — into a timestamped directory outside the
  repo, at tier 2 only. Use when the user says "backup", "back up the station", "export the
  config", "save the config", "snapshot before I change something", or asks whether the station
  can be restored.
---

# Backup

The station's restore point is two files: `settings.json` (all config, with `personas[]` inside it)
and `schedule.json` (`{shows[], schedule, override}`). A backup missing either is not a backup. See
[`AGENTS.md`](../../AGENTS.md) for where those files live on the host and how reads reach them.

## Why the built-in backup is not enough

`GET /api/backup/export` produces a ~95 MB zip containing `settings.json`, `library.db`, skills, sfx,
jingles, avatars and themes — and **no `schedule.json`**. Personas survive a restore because they are
inside `settings.json`; **every show and the entire 7×24 grid do not**. Restoring from that zip gives
back a station with a library and a cast but no programming: it cannot tell you what aired when, and
it cannot put the station back on air as it was. It is a library archive, not a config backup. Do not
call it one.

That is also why this is **tier 2 only**: at tier 1 there is no way to read `schedule.json`, and
`/api/schedule` trims shows to 7 of 26 fields. A tier-1 "backup" would be a file that looks like a
backup and cannot restore the station. **Refuse rather than pretend**, and say exactly this.

## Done when

- `python3 scripts/subwave.py backup` has printed a path outside the repo.
- That path holds both `settings.json` and `schedule.json`, both parse, the grid has 7 days and 168
  cells, and every non-empty cell names a show that exists in the file.
- The human has the path, the counts, and a warning that the files are unredacted.

## Preconditions

1. **Tier 2.** `python3 scripts/subwave.py status` must print tier 2 (HTTP + host access). If it
   prints tier 1, stop and explain the paragraph above — do not substitute the API zip, and do not
   pull with `--allow-lossy` and call the result a backup.
2. `SUBWAVE_HOST` and `SUBWAVE_STATE_DIR` are set — the state dir is the path **inside the
   controller container**, not the host path.
3. A destination outside the repo with room for both files. The tool refuses a path inside the repo,
   and correctly so: these files hold live secrets in plaintext. Put them somewhere private.
4. No off-air window needed. This command only reads. It is safe while the station is live.

## Procedure

```sh
python3 scripts/subwave.py status
```

Record the tier and station name. Then:

```sh
python3 scripts/subwave.py backup
```

The output names the destination. The layout is
`<base>/<station-slug>/<UTC timestamp>/settings.json` and `.../schedule.json`, default
`~/.subwave-backups`. To choose a location:

```sh
python3 scripts/subwave.py backup --dir "$HOME/subwave-backups"
```

`--dir` is a **base** directory, not the final folder; the command creates the station and timestamp
levels under it. Do not pass a path inside this repo. Re-running later never overwrites an earlier
backup — the timestamp is the identity, so take a fresh one rather than assuming the old one is
current.

## Verify

Substitute the printed path:

```sh
BACKUP_DIR="<printed path>" python3 - <<'PY'
import json, os
root = os.environ["BACKUP_DIR"]
settings = json.load(open(os.path.join(root, "settings.json")))
schedule = json.load(open(os.path.join(root, "schedule.json")))
shows = schedule.get("shows") or []
ids = {s.get("id") for s in shows}
grid = schedule.get("schedule") or {}
cells = [c for day in grid.values() for c in day]
print("files     settings.json, schedule.json")
print("personas  %d" % len(settings.get("personas") or []))
print("shows     %d" % len(shows))
print("days      %d" % len(grid))
print("cells     %d/%d filled" % (sum(1 for c in cells if c), len(cells)))
print("orphans   %s" % (sorted({c for c in cells if c and c not in ids}) or "none"))
PY
```

Pass conditions: 7 days, 168 cells, a non-zero show and persona count, and `orphans: none`. A grid
cell naming a show that is not in `shows[]` means the programming references something lost — say so
instead of declaring success.

Then tell the human: the path, those counts, and that the files are **unredacted** and contain
credentials, so the directory deserves the same care as the host.

## Restore (there is no command)

The tool does not restore, and nothing in the repo should hand-edit live files while the controller
runs. Restoring is a deliberate human-approved operation: put `settings.json` back through the admin
API (`POST /api/settings`, whole sections) and `schedule.json` through `PUT /api/schedule`, the same
path a reviewed `apply` uses. The tool does not restore, so say plainly that the backup is verified
and the restore path is manual.

## Failure modes

| Symptom | Cause | Do instead |
| --- | --- | --- |
| Command refuses: "refusing to back up at tier 1" | no host access | explain the missing `schedule.json`; do not offer the zip as a substitute |
| Command refuses: inside the repo | `--dir` pointed into the checkout | choose a path outside; the files hold secrets |
| Backup "worked" but no `schedule.json` | something other than this tool was used | re-run the command; only two files count |
| Grid 168/168 but restores to silence | shows array empty or cell ids orphaned | check `orphans`; report the bad reference |
| Wrong backup restored | oldest timestamp used | take a new one after every config change; the newest stamp wins |
| `instance/` offered as the backup | confusion with `pull` | `pull` is redacted and may be tier-1 lossy; it is not a backup |
| Zero-byte or truncated file | host read failed mid-copy | re-run; verify parse before telling the human it is safe |
| Backup taken, change applied, backup called current | stale | re-run after the change, or say it predates it |
