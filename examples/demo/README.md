# Demo station (fictional)

Everything under `examples/demo/` is invented. **Harbour Lights** is not a real station, the two
hosts are not real people, and the ids are placeholders (`p_demo-nell`, `s_demo-late`), not ids any
server issued.

It exists so the repo is testable with no live station: the shapes here are the same shapes a real
`instance/character/` holds.

| File | Holds |
| --- | --- |
| `character/character.json` | station identity, locale, timezone, theme, house rules |
| `character/personas/*.json` | the host roster |
| `character/shows/*.json` | show briefs |
| `character/schedule.json` | the 7×24 grid, 168 cells, every cell filled |
