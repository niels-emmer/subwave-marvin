# Contributing

Thanks for your interest. This is a small toolkit with one job — operate a SUB/WAVE station
from an agent session — so please keep changes focused and dependency-free.

## Before you start

- For anything non-trivial, open an issue first so we can agree on the approach.
- Security issues must **not** be reported as public issues — see [SECURITY.md](SECURITY.md).

## Development setup

Requires `python3` and nothing else. There is no package to install, no build step and no
runtime dependency beyond the standard library.

```bash
git config core.hooksPath .githooks     # once per clone: enables the pre-commit secret gate
cp .env.example .env                   # only if you want to run against a real station
python3 scripts/subwave.py --help
```

You do not need a station to work on this. `tests/mock_station.py` starts a mock SUB/WAVE
controller in-process on loopback, and `examples/demo/` is a committed fictional station in
the shape a real `instance/` has:

```bash
python3 scripts/subwave.py character init --from demo   # writes examples/demo into instance/
python3 scripts/subwave.py validate --dir examples/demo
```

## Checks

Run before opening a pull request. CI runs the same three, on `ubuntu-24.04`:

```bash
python3 -m unittest discover -s tests   # the whole suite, offline
sh scripts/check-secrets.sh             # tracked files (the commit gate)
python3 scripts/subwave.py validate --dir examples/demo
```

`sh scripts/check-secrets.sh --all` also scans untracked-but-not-ignored files; run it if you
have been generating captures locally.

## Conventions

The rules below are summarised; [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) says what they protect and
why, and its **Invariants** section is the list a change has to survive. If a change cannot keep one of
them, that is a design discussion, not a detail.

- **Standard library only.** No third-party packages. A new dependency is a permanent,
  independently maintained liability; the suite has to run on a bare `python3`.
- **Tests must never need a live station.** No network, no `ssh`, no credentials — that is why
  the mock controller exists. A test that needs a real station belongs behind a flag, not in
  `tests/`.
- **Say which tier you are running at.** Every command prints tier 1 (HTTP only) or tier 2
  (plus host access), and says what it could not read. Never degrade silently.
- **Never read-modify-write from a trimmed read.** `apply` sends whole objects built from the
  repo. Merging from a tier-1 read silently drops the fields the API hides.
- **Unknown fields are warnings, missing fields are errors.** `validate` has to stay callable
  against a station newer than the toolkit.
- **A new write path needs a matching read-back.** HTTP 200 means accepted, not stored.
- **Never commit instance data.** `instance/` and `.env` are gitignored; `examples/demo/` is
  the only station in the repo, and it is fictional.

## Commit and pull request style

Conventional commit subjects (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`), one concern per
pull request, and a body that says what changed and what you ran. The PR template asks for the
checks above — please tick them honestly rather than pre-ticking.
