# Security

`subwave-marvin` is a toolkit for operating a live **SUB/WAVE** radio station from an agent
session. It is not a service: it is a Python script you run yourself, with your credentials,
against your own station. That shape is the whole threat model — the tool is exactly as
trusted as the person running it, and it holds the keys to a station that is on air.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Report it privately via
[GitHub private vulnerability reporting](https://github.com/niels-emmer/subwave-marvin/security/advisories/new).
You will get an acknowledgement within a few days, and a fix is published as a release once
ready.

## Threat model

| Asset | Exposure | Protection |
|---|---|---|
| Station admin password (`SUBWAVE_ADMIN_PASS`), listener password | `.env` on disk, redacted copies in `instance/` | `.env` is gitignored; `check-secrets.sh` runs in the pre-commit hook and CI; `pull` writes `__REDACTED__` in place of every value in `REDACT_PATHS` |
| LLM API keys (`settings.llm.apiKey`, `settings.llm.fallback.apiKey`) | The station's `settings.json`, and any capture of it | Redacted on the way in; rehydrated only in memory at send time |
| This deployment's `instance/` — station identity, personas, shows, the weekly grid, the blocklist | Your working tree | Gitignored, and never present in history; `apply` refuses a payload that still holds `__REDACTED__` |
| Live station state — on air, the programming grid, skills | Any command that writes | `apply` is a dry run unless `--yes`; every air-changing `onair` verb needs `--yes`, and `skip` additionally needs `--expect-id` or `--any-track` |
| SSH access to the station host | Your `ssh` config and agent | Used only at tier 2, non-interactively (`BatchMode=yes`, `ConnectTimeout=8`); the toolkit never adds, edits or stores ssh material |
| `library.db`, backups and archives | The station's state directory | Never copied unless you pass `--with-library`; gitignored in this repo |

## What the tool does to keep those safe

- **Nothing is written without an explicit flag.** `apply` prints the changes and sends
  nothing until `--yes`; deletions are never a side effect of a sync.
- **`apply` runs `validate` first and refuses on any error**, takes a backup before writing,
  then re-reads the station and fails loudly if a field did not land.
- **`apply` refuses a lossy capture.** A tier-1 `pull` cannot see the fields the API trims,
  so it records `capture.lossy` and `apply` refuses to push that back rather than strip
  every show to seven fields.
- **Secrets are named, never printed.** Verification reports a secret path structurally
  (`_is_secret_path`) rather than echoing an expected or actual value, and `check-secrets.sh`
  masks a matched value before it prints a hit.
- **The toolkit does not bypass the station's own review step.** A station skill's `tool.mjs`
  is quarantined until a human posts its sha256 to the trust endpoint; nothing here installs
  or trusts it.
- **Config and live control stay separate.** Config goes through the admin HTTP API; on-air
  changes go through MCP. On-air state is never captured into git, and config is never
  written through MCP.

## Accepted risks — stated plainly

These are deliberate, documented trade-offs rather than oversights:

1. **A `.env` holds live credentials in plaintext.** It is gitignored, not encrypted.
   Anything that can read that file can administer your station.
2. **The toolkit runs with your privileges.** At tier 2 that includes `ssh` to the station
   host and `docker exec` inside the controller container. There is no sandbox; a host entry
   you add is a host this tool can reach.
3. **Tier 1 is lossy on purpose.** The documented API trims persona and show definitions and
   there is no `GET /shows`, so a tier-1 `pull` is incomplete by design and the tool says so
   instead of pretending otherwise.
4. **The station's own backup export omits `schedule.json` and `blocklist.json`.** A stock
   export/restore therefore loses every show, the whole grid and every ban. This toolkit
   compensates by reading those separately, but anything you restore from a stock export
   will still be missing them.
5. **On-air actions are ephemeral.** `onair` changes what is playing now and is not recorded
   in git or in `pull`/`diff`/`apply`. `docs/applied.md` is a human-authored audit trail, not
   an enforced log.
6. **Some settings apply only on a mixer restart** (`privacy.listenerAuth`, `stream.maxListeners`,
   `opus`, `aac`). A restart costs seconds of stream interruption; the toolkit tells you which
   changes need one rather than restarting behind your back.

## Dependencies

There are none at runtime: the toolkit is Python 3 standard library only, and CI runs on a bare
`ubuntu-24.04` runner with no install step. That decides what is worth turning on:

- **Dependabot watches GitHub Actions versions only.** `.github/dependabot.yml` has a single
  `github-actions` entry because there is no pip ecosystem to watch.
- **Dependabot security updates are disabled on purpose.** With no runtime dependency graph there is
  nothing for them to patch. Leaving them enabled would show a green "security updates on" signal
  that can never fire, which is worse than an honest off. If a runtime dependency is ever added,
  turn them on in the same change.
- **Secret scanning uses the provider patterns** (GitHub, AWS and the rest) **plus push protection.**
  No custom patterns are configured: this repo has no credential format of its own to match, and
  `scripts/check-secrets.sh` covers the project-specific cases locally and in CI.

## Out of scope

The station itself — its authentication, its Icecast and Liquidsoap configuration, and the
container image — is a separate project. Report issues in those against their own repository.
This toolkit assumes the station enforces its own access control and that you reach it over
a trusted network or a tunnel.
