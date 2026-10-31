## What

<!-- A short description of the change. -->

## Why

<!-- The problem it solves. Link issues with "Closes #123". -->

## How

<!-- Notable implementation details or trade-offs. -->

## Checklist

- [ ] `python3 -m unittest discover -s tests` passes
- [ ] `sh scripts/check-secrets.sh` is clean
- [ ] `python3 scripts/subwave.py validate --dir examples/demo` passes
- [ ] No instance data, credentials, hostnames or generated captures included
- [ ] Docs updated if behaviour or a guard changed (`AGENTS.md`, `docs/`, `README.md`)
- [ ] New write paths re-read the station afterwards and report a value that did not land
- [ ] Any new command still announces its tier, and says what it could not read
