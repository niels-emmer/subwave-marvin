# Public readiness

For the human turning this repository from private to public. Work top to bottom: items 1–3 are
unrecoverable once the switch is flipped, items 4–5 are the settings that guard what is already out
there. Run everything from the repo root of a clean `main`.

## 1. Confirm no instance data is tracked

```sh
git grep -nIE 'p_[0-9a-f]{6}|s_[0-9a-f]{6}'        # server-issued persona and show ids
```

Exit status 1 and no output is the pass: no real persona or show id appears in a tracked file. Then
sweep for deployment values:

```sh
git grep -nIE '([0-9]{1,3}\.){3}[0-9]{1,3}|SUBWAVE_(HOST|BASE_URL|STATE_DIR)=.+|[A-Za-z0-9-]+\.(ts\.net|local|lan)' -- ':!tests'
```

Expected output is one line: the loopback address in the `.env.example` comment. Anything else — a
hostname, an ssh alias, a routable IP, a non-empty deployment value — is a finding.

**Why:** tracked files are exactly the bytes GitHub serves, so this is the whole public surface. Ids
matter most because they are unguessable: an id in the repo points at one real station, and a persona or
show name is enough to recognise whose.

## 2. Confirm `.env` and `instance/` are ignored — and were never committed

```sh
git ls-files .env instance        # must print nothing
git check-ignore -v .env instance/example.json
git log --all -- .env instance    # must print nothing
```

**Why:** ignore rules only stop future commits; only history proves nothing ever got in. `--all` is
what makes that check honest — it covers remote-tracking refs, not just your local branch.

## 3. Rotate every credential that ever touched a commit

If item 2 was not clean, treat every value in those commits as burned: admin password, station password,
tokens, any ssh material. Change them on the station, update `.env`, and enable the local gate so it does
not happen twice (see the last section).

**Why:** publishing history is permanent. Clones, forks, GitHub's API and caches, and mirrors keep the
old objects. Deleting the value in a later commit does not unpublish it, and rewriting history is not a
fix either: forks and earlier clones are untouched, and the rewrite itself is a new risk.

## 4. Turn on GitHub secret scanning and push protection

Settings → Code security and analysis: enable **secret scanning** and **push protection**.

**Why:** this is the only control enforced server-side, for every clone, every web-UI edit and every bot.
The repo's own `scripts/check-secrets.sh` runs on the committer's machine and in CI after the object
already exists.

## 5. Protect `main`, requiring CI

Settings → Branches → rule on `main`: require a pull request, require all three checks from
`.github/workflows/ci.yml` — `secrets`, `tests`, `demo` — and block force pushes.

**Why:** an unprotected `main` lets a direct push skip those jobs, which makes "CI is green" mean
nothing to a visitor reading the repo. Require all three, not just `secrets`: a regression in
`tests` or in the committed demo is exactly what this repo's CI exists to catch.

**This one needs the flip first.** On a free plan, rulesets and branch protection return
`403 Upgrade to GitHub Pro or make this repository public` while the repo is private. Do item 9,
then this one, then secret scanning (item 4).

## 6. Decide: fictional demo cast, or a screenshot

`examples/demo/` is a committed fictional station in the same shape a real `instance/` has. Decide whether
that carries the README or whether you want a screenshot as well.

**Decided: the demo cast carries it, no screenshot.** This toolkit has no interface of its own — it is a
CLI and a set of agent procedures — so there is nothing to capture but the station's admin console, whose
UI belongs to a different project and whose pixels are the easiest way to publish a hostname by accident.
`examples/demo/` already shows the shape a real station has, and the README's sample session shows the
interaction, which is the part a screenshot cannot.

**Why:** a cold reader needs to see what a station looks like and prose alone under-sells it. But a
screenshot is the easiest way to publish a hostname, an IP or a real host name by accident. If you take
one, take it against a station populated only from demo data, and eyeball it — the item 1 greps cannot
read inside a PNG.

## 7. Check the README renders without the private repo's assumptions

Read `README.md` as a stranger: the clone URL resolves publicly, every relative link (`AGENTS.md`,
`docs/PLAN.md`, `skills/`) exists on `main`, and there are no absolute local paths, internal hostnames or
references to a companion repo that will not be public. Then confirm every command it names is real:

```sh
python3 scripts/subwave.py --help
```

The commands are `status`, `pull`, `diff`, `validate`, `apply`, `backup`, `restore`,
`character`, `blocklist`, `skills` and `onair` — the README appendix lists all eleven with their
flags, and `--help` is the authority.

**Why:** private-repo habits are invisible to the author and are the first thing a visitor trips over —
a link to a file that was never committed, or a clone command for a different remote.

## 8. Confirm the LICENSE matches intent

`LICENSE` is MIT, copyright 2026 Niels Emmer.

**Why:** upstream **SUB/WAVE** is MIT, so MIT keeps the two compatible and lets someone fork this toolkit
for their own station. A missing or mismatched license makes the repo legally unusable for exactly the
audience it is written for.

## 9. Flip visibility last

Do 1–8, then make the repository public.

**Why:** 1–3 cannot be undone after the switch, and 4–5 protect nothing until the code is actually out.

## Author identity in the history

Two things in the commit history are visible the moment the repo goes public, and both are cheap
to fix now and awkward to fix later:

- The **root commit** (`7e7f66d`) is authored `Agent Orchestrator <ao@example.com>` — a
  placeholder. `git log` shows it to anyone who looks.
- Most commits carry `Niels Emmer <n.emmer@teamrockstars.nl>`, a work address, alongside
  `niels <80741632+niels-emmer@users.noreply.github.com>`.

Neither leaks a credential, so neither blocks the flip. Fixing either means rewriting every
commit (`git filter-repo`), which invalidates the 21 merged PRs that point at the old SHAs and
every existing clone. **Decide deliberately**: publish as-is and accept the placeholder on the
root commit, or rewrite before the flip and accept that the PR history becomes dangling. Do not
rewrite afterwards.

## The local guard is not the control

`scripts/check-secrets.sh` is a guard, not a substitute for GitHub-side protection. It scans tracked
files (untracked-but-not-ignored with `--all`), it is skipped by `git commit --no-verify`, and it cannot
see inside binaries. Turn it on once per clone:

```sh
git config core.hooksPath .githooks
```

GitHub's push protection is the control: server-side, for every clone, and not skippable by whoever is
pushing.
