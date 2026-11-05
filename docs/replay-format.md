# Official replay format — findings so far

`replay.dat` written by `aicup2020 --save-replay` is **not compressed** (gzip shrinks it 5x) and
uses the same binary `trans` encoding as the TCP protocol: little-endian `i32`, `bool` as one byte,
`Option` as a 0/1 tag byte, `Vec`/map as `i32 count` + items, strings as `i32 len` + UTF-8.

The file starts with the full initial game state, the engine's `Model` struct (11 fields, names from
the binary's serde strings): `next_id`, `map_size`, `fog_of_war`, `entity_properties`
(map `EntityType -> EntityProperties`, identical to the client's), `max_tick_count`,
`max_pathfind_nodes`, `current_tick`, `passive_income`, `players`, `entities`, `entity_actions`.
`server/tools/replay_probe.py` parses it up to `players` with the official Python client's readers.

Open points:

- `Player` in the server model has **4** fields (`struct Player with 4 elements`; client has 3:
  id, score, resource — the extra one is probably `index`), so the probe's player/entity parse is
  off by that field. `Entity` has 6 fields like the client. `entity_actions` is a map
  `id -> StoredAction {entity_id, action}`.
- After the initial state comes the per-tick history, ~0.8 KB per tick: most likely a `Vec` of diffs
  produced by batbox's `Diff` derive (per-field diffs for structs, id-keyed change lists for the
  `Collection` of entities). Decoding it means reverse-engineering that derive; verify against the
  GUI and against `results.json` scores at the last tick.
- Alternative that needs no reverse engineering: record our own replay with a TCP proxy between the
  engine and every strategy (every `PlayerView` + every `Action`). Exact for Round1 (no fog); for
  fog games the union of all players' views is what a spectator gets.
