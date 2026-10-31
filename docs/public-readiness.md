# Public launch record

This repository went public on **2026-10-31** with release **v1.0.0**. This document started as a
pre-flight checklist for that switch; it is now the record of what was done and the list of checks
worth re-running before any future change widens the public surface.

The flip, branch protection and secret scanning all came last on purpose: on a free plan, branch
protection and secret scanning both return `403 Upgrade to GitHub Pro or make this repository public`
while the repo is private, so the settings that guard what is already out there can only be applied
once it *is* out there.

## State at publication

| What | Value |
|---|---|
| Visibility | public |
| Default branch | `main` |
| License | MIT, detected |
| Community profile | 100% — README, LICENSE, SECURITY, CONTRIBUTING, CODE_OF_CONDUCT, PR template |
| Release | `v1.0.0`, tagged from `main` after CI was green |
| Branch protection on `main` | requires `secrets`, `tests`, `demo`; strict; 1 approval; no force-push; no deletion |
| Secret scanning | enabled |
| Push protection | enabled |

Re-verify the settings that matter:

```sh
gh repo view niels-emmer/subwave-marvin --json visibility,isSecurityPolicyEnabled
gh api repos/niels-emmer/subwave-marvin/community/profile --jq .health_percentage
gh api repos/niels-emmer/subwave-marvin/branches/main/protection \
  --jq .required_status_checks.contexts
gh api repos/niels-emmer/subwave-marvin \
  --jq '.security_and_analysis | {secret_scanning: .secret_scanning.status,
        push_protection: .secret_scanning_push_protection.status}'
```

## 1. No instance data is tracked

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

## 2. `.env` and `instance/` are ignored — and were never committed

```sh
git ls-files .env instance        # must print nothing
git check-ignore -v .env instance/example.json
git log --all -- .env instance    # must print nothing
```

**Why:** ignore rules only stop future commits; only history proves nothing ever got in. `--all` is
what makes that check honest — it covers remote-tracking refs, not just your local branch.

## 3. No credential ever touched a commit

Checked clean before publication, so nothing needed rotating. If a later sweep ever finds one, treat
every value in those commits as burned: admin password, station password, tokens, any ssh material.
Change them on the station, update `.env`, and turn on the local gate (see the last section).

**Why:** publishing history is permanent. Clones, forks, GitHub's API and caches, and mirrors keep the
old objects. Deleting the value in a later commit does not unpublish it, and rewriting history is not a
fix either: forks and earlier clones are untouched, and the rewrite itself is a new risk.

## 4. Secret scanning and push protection

Settings → Code security and analysis: **secret scanning** and **push protection** are on.

**Why:** this is the only control enforced server-side, for every clone, every web-UI edit and every bot.
The repo's own `scripts/check-secrets.sh` runs on the committer's machine and in CI after the object
already exists.

## 5. `main` is protected and requires CI

Settings → Branches → rule on `main`: require a pull request, require all three checks from
`.github/workflows/ci.yml` — `secrets`, `tests`, `demo` — and block force pushes.

**Why:** an unprotected `main` lets a direct push skip those jobs, which makes "CI is green" mean
nothing to a visitor reading the repo. Require all three, not just `secrets`: a regression in
`tests` or in the committed demo is exactly what this repo's CI exists to catch.

`enforce_admins` is **off** because there is one maintainer: non-admins get the full gate, while the
owner's own merges bypass it and GitHub prints `Bypassed rule violations …`. That is expected, not an
error. It also means a required approval cannot come from the author, so the owner's merges always take
the bypass path.

## 6. The demo cast carries the README; there is no screenshot

`examples/demo/` is a committed fictional station in the same shape a real `instance/` has, and it
carries the README.

**Why no screenshot:** this toolkit has no interface of its own — it is a CLI and a set of agent
procedures — so the only thing to capture is the station's admin console, whose UI belongs to a
different project and whose pixels are the easiest way to publish a hostname by accident. The README's
sample session shows the interaction, which is the part a screenshot cannot. If you ever do take one,
capture a station populated only from demo data and eyeball it — the item 1 greps cannot read inside a
PNG.

## 7. The README reads for a stranger

The clone URL resolves publicly, every relative link (`AGENTS.md`, `docs/PLAN.md`, `skills/`) exists on
`main`, and there are no absolute local paths, internal hostnames or references to a repo that is not
public. Every command it names is real:

```sh
python3 scripts/subwave.py --help
```

The commands are `status`, `pull`, `diff`, `validate`, `apply`, `backup`, `restore`, `character`,
`blocklist`, `skills` and `onair`. The README appendix lists all eleven with their flags, and `--help`
is the authority.

**Why:** private-repo habits are invisible to the author and are the first thing a visitor trips over —
a link to a file that was never committed, or a clone command for a different remote.

## 8. LICENSE matches intent

`LICENSE` is MIT, copyright 2026 Niels Emmer.

**Why:** upstream **SUB/WAVE** is MIT, so MIT keeps the two compatible and lets someone fork this toolkit
for their own station. A missing or mismatched license makes the repo legally unusable for exactly the
audience it is written for.

## Sweeping a public repository means every branch

This is the trap that a private-repo habit hides: **publication exposes every branch, not just
`main`.** A leak on an abandoned feature branch is as public as one on the default branch, and the
community profile, the README and a casual look at `main` all say nothing about it.

The pre-publication sweep must therefore cover remote refs too:

```sh
# never committed, on any ref
git log --all --oneline -- '.env' 'instance/'        # must print nothing

# key formats, on every remote branch
git grep -hInE 'sk-[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|xox[baprs]-|BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16}' \
  $(git for-each-ref --format='%(refname)' refs/remotes/origin | grep -v HEAD)

# hostnames and routable IPs, on every remote branch
git grep -hInE '([0-9]{1,3}\.){3}[0-9]{1,3}|[A-Za-z0-9-]+\.(ts\.net|local|lan)' \
  $(git for-each-ref --format='%(refname)' refs/remotes/origin | grep -v HEAD) -- ':!tests'
```

All three were clean across all branches at publication.

## Author identity in the history

Recorded here because it is visible to anyone who runs `git log`:

- The **root commit** (`7e7f66d`) is authored `Agent Orchestrator <ao@example.com>` — a placeholder.
- Most commits carry `Niels Emmer <n.emmer@teamrockstars.nl>`, a work address, alongside
  `niels <80741632+niels-emmer@users.noreply.github.com>`.

**Decided: published as-is.** Neither leaks a credential. Fixing either would mean rewriting every
commit (`git filter-repo`), which invalidates the merged PRs that point at the old SHAs and every
existing clone. That trade was not worth a cosmetic placeholder.

## The local guard is not the control

`scripts/check-secrets.sh` is a guard, not a substitute for GitHub-side protection. It scans tracked
files (untracked-but-not-ignored with `--all`), it is skipped by `git commit --no-verify`, and it cannot
see inside binaries. Turn it on once per clone:

```sh
git config core.hooksPath .githooks
```

GitHub's push protection is the control: server-side, for every clone, and not skippable by whoever is
pushing.
