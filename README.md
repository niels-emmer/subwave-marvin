# subwave-marvin

You run a SUB/WAVE station. This lets you look after it by talking to your agent instead of clicking
through the admin console. Your station's settings live as files in a git repo, so you can see what
changed, undo it, and keep a history of how the station got to sound the way it does.

## What you can ask for

### Finding out what is going on

**"What's on air right now?"**
The track, the show, the host, and whether the station is actually streaming.

**"What changed since last week?"**
Compares your files against the live station and lists what differs, by host, by show, by hour of the
week. Asking changes nothing.

### Fixing how a host sounds

**"Why does <host> talk so much?"**
Reads that host's dials, their written character and the shows they present, then proposes the smallest
change that would fix it. It does not guess: if the answer is "that show is set to talk a lot", it says so
and shows you the setting.

**"Make <host> warmer"** or **"make this show less chatty"**
Changes that one setting, checks the result, and shows you before and after. Nothing goes live until you
say so.

### Adding people and programmes

**"Add a host"**
Scaffolds a new host — but it asks you to describe the character first, because a host's soul *is* the
prompt the DJ writes from. A blank soul produces a blank presenter.

**"Make me a new show"**, then **"put it on Sunday mornings"**
Creates the show and fills the hours you name in the weekly grid, showing you which cells it changes and
which it would take from another show.

### Keeping things out

**"Stop playing <artist>"**
Adds a ban. Bans apply to the whole station, or only while a particular show is on air.

**"Block Christmas music until December"**
Worth reading twice: a seasonal rule's window is **when the rule is paused**, not when it applies. A
December window means the ban is off during December and on for the other eleven months. If you want it the
other way round, say so.

### Protecting the station

**"Back up the station"**
Copies the settings, the shows, the schedule, the station's skills and its media manifests, and writes a
note saying where each file goes back. Deliberately more than the console's export, which leaves out the
schedule — and so loses every show and the whole grid.

**"Put the station back to what's in git"**
Sends your files to the station. It shows you every change first and sends nothing until you confirm.

**"Undo the last change"**
Puts a backup back, or applies an earlier commit — depending on whether you want the station as it was ten
minutes ago or last week.

### Live, right now

**"Say something on air"**, **"skip this track"**, **"play a jingle"**
These change what is happening this second. They are the only things here that do, they need your explicit
go-ahead each time, and a skip also needs to know which track you meant — or your permission to skip
whatever is playing.

## What it cannot do yet

- **Avatars and station imaging.** Jingles, sound effects and beds are backed up but not authored here. Use
  the station's admin console for those.
- **Library work.** Tagging, building playlists, searching your collection — that is Navidrome's job.
- **Anything that bypasses the station's review step for a skill's custom script** — the station refuses to
  run one until a person has read and trusted it, and nothing here shortcuts that.

## What a session looks like

```
You:    add a host
Agent:  Happy to. What is this host like - name, temperament, how they talk?
You:    Ada Vance. Dry, unhurried, notices small things, never oversells a record.
Agent:  Here is the host it built, with a soul in her voice. Before I put her on air,
        which show should she present?
You:    Sunday mornings
Agent:  That fills Sun 07:00-10:00, taking one hour from Late Harbour. Nothing is live yet.
        Say the word and I will push it.
You:    go ahead
Agent:  Applied, then read back from the station: one host, one show, three grid cells confirmed.
```

## How it works

Your station is described by the files in `instance/`. That directory is yours alone — it is excluded from
git, so nothing about your station is ever published. Your agent edits those files, shows you the diff, and
sends them only when you say so.

```
   instance/  ──edit──►  diff  ──you approve──►  apply  ──►  the station
   your files             what would change       sends nothing until you say yes
```

A few things work the other way round, because waiting for a file edit makes no sense when the DJ is
mid-sentence:

```
   "say something on air"  ──►  the station      (no file, no review, gone in a minute)
```

That second path is the only one that skips the review, and it never touches your files or your history.

## Getting started

You need `python3` (nothing to install), your station, and its admin username and password. Copy
`.env.example` to `.env` and fill it in, then ask your agent for the station's status.

No station yet? `character init --from demo` writes a small fictional station into `instance/`, so you can
walk the whole flow — edit, validate, dry run — without one.

One honest note. With only the station's web address you get a partial picture: the API trims host and show
details, and a backup cannot include your shows or the weekly grid. If your agent can also reach the machine
the station runs on, tell it the ssh host and the data directory — then you get the whole thing, including a
backup that can restore the programming. Worth doing before you change anything.

## Safety, short

Nothing is written to your station until you say so, and a backup is taken first. Every write is read back
afterwards, so a change that did not land is reported rather than assumed. Deletions refuse when something
still depends on what you are deleting. The full list of guards, and what each protects against, is in
`AGENTS.md`.

## Appendix: the technical reference

Two ways to reach a station. The second is worth having.

|  | Web address only | Plus ssh access |
| --- | --- | --- |
| Needs | address + admin password | also ssh and `docker exec` |
| Read hosts and shows | trimmed | complete |
| Read the weekly grid | yes | yes |
| Backup | **without the schedule** | complete |

| Command | What it does | Example |
| --- | --- | --- |
| `status` | tier, station, what is on air, whether your files match | `status` |
| `pull` | copy the station into `instance/` | `pull --force` |
| `diff` | what differs between your files and the station | `diff` |
| `validate` | check your files before applying | `validate --dir examples/demo` |
| `apply` | send your files to the station; dry run unless `--yes` | `apply --yes` |
| `backup` | config, assets and schedule, outside the repo | `backup --with-library` |
| `restore` | put a backup back | `restore <dir> --yes` |
| `character` | hosts, shows and the weekly grid | `character new-show <slug> --persona <persona>` |
| `skills` | the station's own DJ segments | `skills new late-shift` |
| `blocklist` | bans and rules | `blocklist block --type artist --id <id>` |
| `onair` | live control | `onair say "Back after this." --yes` |

Flags live here rather than in the body: `apply` also takes `--force-now` (write while on air), `--no-backup`,
`--backup-dir`, and `--prune` / `--force-prune` for things on the station but not in your files. `pull` takes
`--allow-lossy` for a partial copy. Every `onair` verb that changes air needs `--yes`; `skip` needs
`--expect-id` or `--any-track`.

Two directories are called `skills/`, and they are different things:

- `skills/` at the top of the repo — the procedures your agent follows when it works here.
- `instance/skills/` — your station's own DJ segments, the ones that go to air.

## Where to read more

`AGENTS.md` for agents and the full guard list · `docs/PLAN.md` for design · `docs/decision-log.md` for why · `docs/applied.md` for what has aired.
