# subwave-marvin

You run a SUB/WAVE station. This lets you look after it by talking to your agent instead of clicking
through the admin console. Your station's settings live as files in a git repo, so you can see what
changed, undo it, and keep a history of how the station got to sound the way it does.

## What you can ask for

### Finding out what is going on

**"What's on air right now?"**
Tells you the track, the show and the host, and whether the station is actually streaming.

**"What changed since last week?"**
Compares your files against the live station and lists what differs — by host, by show, by grid cell.
Nothing is changed by asking.

### Fixing how a host sounds

**"Why does <host> talk so much?"**
Reads that host's dials, their written character and the shows they present, then proposes the smallest
change that would fix it. It does not guess: if the answer is "the show is set to talk a lot", it says so
and shows you the field.

**"Make <host> warmer"** or **"make this show less chatty"**
Changes that one setting, checks the result, and shows you the before and after. Nothing goes live until
you say so.

### Adding people and programmes

**"Add a host"**
Scaffolds a new host — but it will ask you to describe the character first, because a host's soul *is* the
prompt the DJ writes from. A blank soul produces a blank presenter.

**"Make me a new show"**, then **"put it on Sunday mornings"**
Creates the show and fills the hours you name in the weekly grid. It will show you which cells it is about
to change, including any it would take from another show.

### Keeping things out

**"Stop playing <artist>"**
Adds a ban. Bans apply to the whole station, or only while a particular show is on air.

**"Block Christmas music until December"**
This one is worth reading twice. A seasonal rule's window is **when the rule is paused**, not when it
applies — so a December window means the ban is off during December and on for the other eleven months.
If you want the opposite, say so and the window goes the other way round.

### Protecting the station

**"Back up the station"**
Takes a copy of the settings, the shows, the schedule, the station's skills and its media manifests — and
writes a note saying where each file goes back. This is deliberately more than the console's export, which
leaves out the schedule and therefore every show and the whole grid.

**"Put the station back to what's in git"**
Applies your files to the station. It shows you every change first and sends nothing until you confirm.

**"Undo the last change"**
Puts a backup back, or applies an earlier commit — your choice, depending on whether you want the station
as it was ten minutes ago or as it was last week.

### Live, right now

**"Say something on air"**, **"skip this track"**, **"play a jingle"**
These change what is happening this second. They are the only things in the toolkit that do, they need your
explicit go-ahead each time, and a skip additionally needs to know which track you meant — or your
permission to skip whatever is playing.

## What it cannot do yet

- **Avatars and station imaging.** Jingles, sound effects and beds are backed up but not authored here.
  Use the station's admin console for those.
- **Library work.** Tagging, building playlists, searching your collection — that is Navidrome's job, and
  the station's own tools do it better than a config repo could.
- **Anything that bypasses the station's review step for a skill's custom script.** A skill can carry a
  small script, and the station deliberately refuses to run one until a person has read it and trusted it.
  Nothing here shortcuts that, and nothing should.

## Getting started

You need three things: the station's address, its admin username and its admin password. Put them in a
`.env` file (copy `.env.example`), then ask your agent for the station's status.

If your agent can also reach the machine the station runs on, tell it the ssh host and the station's data
directory. That unlocks the fuller picture: complete show definitions, and a backup that can actually
restore the station. Without it you get a trimmed view and a backup that cannot bring the programming back.

---

## Appendix: the commands, for the curious

Everything above is the agent driving these. You can run them yourself.

| Command | What it does |
| --- | --- |
| `status` | tier, station, what is on air, whether your files match the station |
| `pull` | copy the station's settings into `instance/` (`--force` replaces, `--allow-lossy` accepts a partial copy) |
| `diff` | list what differs between your files and the station |
| `validate` | check your files before applying them (`--dir` for another station directory) |
| `apply` | send your files to the station; dry run unless `--yes` |
| `backup` | config, assets and schedule, outside the repo (`--with-library` adds the audio analysis) |
| `restore` | put a backup back; dry run unless `--yes` |
| `character` | create and edit hosts, shows and the weekly grid |
| `skills` | the station's own DJ segments: list, new, show, edit, rm |
| `blocklist` | bans and rules: list, block, unblock, rm, rule add/edit |
| `onair` | live control: tools, now, state, say, segment, skip, request, queue, sfx, jingle, list |

`apply` also takes `--force-now` to write while a show is on air, `--no-backup` to skip the backup, and
`--prune` / `--force-prune` to delete things that exist on the station but not in your files.

## Where to read more

`AGENTS.md` for agents · `docs/PLAN.md` for design · `docs/decision-log.md` for why things are the way they
are · `docs/applied.md` for what has gone on air.
