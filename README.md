# subwave-marvin

[![CI](https://github.com/niels-emmer/subwave-marvin/actions/workflows/ci.yml/badge.svg)](https://github.com/niels-emmer/subwave-marvin/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

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
schedule — and so loses every show and the whole grid. It needs host access: without it the tool refuses
rather than hand you a backup that could not restore the programming.

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

## Without a checkout: the station's own MCP server

If you only want to talk to the station — what is on air, say something, skip a track, queue a request —
you do not need this repository at all. The station serves an MCP endpoint at `/api/mcp` (Streamable HTTP,
21 tools), so any MCP-capable agent can be pointed at it as a server. Reads need no credentials; the
control tools take the station's admin username and password.

What that door does not have is every safety property below it in this README: no diff, no dry run, no
backup before a change, no read-back afterwards, no `--yes`. Each call happens immediately and cannot be
undone. And it cannot read or write the station's configuration at all — no persona, show, schedule or
settings tool exists there. That is what the checkout, and `apply`, are for.

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

```sh
git clone https://github.com/niels-emmer/subwave-marvin.git
cd subwave-marvin
cp .env.example .env        # the station's address and its admin password
```

Then open your agent in that directory — Claude Code, Codex, OpenCode, or anything else that reads
`AGENTS.md` — and ask for the station's status. Nothing to install: the tool is one Python file with no
dependencies.

No station yet? `character init --from demo` writes a small fictional station into `instance/`, so you can
walk the whole flow — edit, validate, dry run — without one.

### One choice decides how much you get

**With only the web address** you can author a station from scratch and push it — with `apply --yes
--no-backup`, because a backup is the one thing that tier cannot take. You cannot take over the one you
already have: the API trims host and show details, and it cannot hand back your shows or the weekly grid at
all. A web-address-only copy of a live station is marked lossy, and `apply` refuses it rather than push back
seven fields per show and lose the other nineteen.

**Add ssh access** and you get the whole station: full reads, a capture you can edit, and a backup that can
restore the programming.

You do not have to work out the rest yourself. Set `SUBWAVE_HOST` to your ssh address and run status: it
finds the controller container and its data directory on the host and prints the two lines to add to
`.env`.

## Safety, short

Nothing is written to your station until you say so, and a backup is taken first (at tier 1 there is no
backup to take, and `apply` refuses unless you pass `--no-backup`). Every write is read back afterwards, so
a change that did not land is reported rather than assumed. Deletions refuse when something still depends
on what you are deleting. The full list of guards, and what each protects against, is in `AGENTS.md`.

## Appendix: the technical reference

Two ways to reach a station. The second is worth having.

|  | Web address only | Plus ssh access |
| --- | --- | --- |
| Needs | address + admin password | also ssh and `docker exec` |
| Create / update | author from scratch only, with `--no-backup` | yes |
| Read hosts and shows | trimmed | complete |
| Read the weekly grid | yes | yes |
| Backup | **refused** | complete |

| Command | What it does | Example |
| --- | --- | --- |
| `status` | tier, station, what is on air, drift vs your files (tier 2 only), and the lines that reach host access | `status` |
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
`--allow-lossy` for a partial copy. `backup --dir` writes the copy somewhere other than `~/.subwave-backups`,
and `restore --force-now` restores while the station is on air. `character init` takes `--from demo|blank` — a
placeholder station instead of the demo — and `--force` to replace an existing `instance/`. `skills edit`
changes `--label`, `--cooldown` and `--brief`, and `blocklist rule add|edit` authors a rule by slug.

The `onair` reads are free: `onair tools`, `onair now`, `onair state`, and `onair list skills|sfx|jingles`.
Every `onair` verb that changes air needs `--yes`: `say` (with `--raw` to say it verbatim, `--over-track` to
talk over the music, and `--sfx` to play an effect first), `segment`, `skip`, `request`, `queue` (with `--pick N`
to take a result other than the top one), `sfx` and `jingle`. `skip` additionally needs `--expect-id` or
`--any-track`.

Two directories are called `skills/`, and they are different things:

- `skills/` at the top of the repo — the procedures your agent follows when it works here.
- `instance/skills/` — your station's own DJ segments, the ones that go to air.

## Where to read more

`AGENTS.md` for agents and the full guard list · `docs/ARCHITECTURE.md` for how it is built · `docs/PLAN.md` for what is left · `docs/decision-log.md` for why · `docs/applied.md` for what has aired.
