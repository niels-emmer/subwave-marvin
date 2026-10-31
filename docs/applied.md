# Applied changes — on-air audit trail

**Rule: no apply lands on a live station without a row here.** Append the row at the time of the apply,
never edit a past one.

`Backup ref` points at the `settings.json` + `schedule.json` copy taken **outside this repo** immediately
before the apply (they contain secrets), e.g. `~/subwave-backups/2026-11-02T1015Z/`. `Listener check` is
the result of one real listener connect afterwards. `Commit` is the repo revision the tool was run from;
a station-side change has no commit of its own, so `—` means no revision was recorded.

| Date | Commit | What changed | Backup ref | Verified by | Listener check |
| --- | --- | --- | --- | --- | --- |
| 2026-10-31 | — | `character.json` houseRules → `settings.djHouseRules`: six station-wide rules (British English and in character; no political/medical/legal/financial advice; never spoil the Ultimate Answer; links under ~45s; no repeated track or reading back listener details; no format talk over instrumentals). The station had no global tone guardrails. | `~/.subwave-backups/marv-s-tunes/20261031T095542Z` (settings, schedule, assets, library) | post-apply readback of the rehydrated payload; `diff` in sync across 4 personas / 6 shows / 168 cells / 44 settings keys; `/api/health` on air | stream healthy, station on air |
