# Official replay format (`aicup2020 --save-replay`) — decoded

`server/codewars/replay.py` reads it; `./cw replay <game id>` validates a file and can export JSON;
the web viewer (`/games/<id>`, `static/viewer.js`) consumes `/games/<id>/replay.json`. Verified on
every stored replay: the file parses to the last byte and the scores at the final tick equal
`results.json`.

## Encoding

The engine's `trans` encoding, the same as on the TCP wire: little-endian `i32`, `bool` = one byte,
`Option<T>` = tag byte (0/1) + value, `Vec<T>` = `i32` count + items, strings = `i32` length + UTF-8.
`usize` fields are written as `i32`. The file is not compressed.

## Layout

```
Model                               -- the full state at tick 0
repeat until EOF (one record per tick):
    Vec<Event>                      -- events that happened during the tick
    ModelDelta                      -- batbox `Diff` of the model against the previous tick
```

`Model` (11 fields, in this order):

| field | type |
|-------|------|
| `next_id` | i32 |
| `map_size` | i32 |
| `fog_of_war` | bool |
| `entity_properties` | map `EntityType -> EntityProperties` (count, then key i32 + value; same as the client's `EntityProperties`) |
| `max_tick_count` | i32 |
| `max_pathfind_nodes` | i32 |
| `current_tick` | i32 |
| `passive_income` | i32 |
| `players` | Collection<Player> |
| `entities` | Collection<Entity> |
| `entity_actions` | Collection<StoredAction> |

A `Collection<T>` is written like a map: `i32` count, then `id` (i32) + `T` for every item.

- `Player` = `id`, `index` (seat, 0-based), `score`, `resource` — four i32 (the client sees three).
- `Entity` = `id` i32, `player_id` Option<i32>, `entity_type` i32, `position` (x i32, y i32), `health` i32,
  `active` bool — identical to the client's.
- `StoredAction` = `entity_id` i32 + `EntityAction` (move/build/attack/repair as `Option`s, identical to
  the client's `EntityAction`).

`ModelDelta` repeats the scalar fields and skips the properties map:

```
next_id i32, map_size i32, fog_of_war bool, max_tick_count i32, max_pathfind_nodes i32,
current_tick i32, passive_income i32,
CollectionDelta<Player>, CollectionDelta<Entity>, CollectionDelta<StoredAction>
```

`CollectionDelta<T>` = `added: Vec<T>`, `removed: Vec<id>`, `updated: Vec<(id, T)>` — in that order
(a changed item is re-sent whole; the per-field diff of a struct of primitives is the struct).

`Event` = tag i32 + two i32. Only tag 0 has been observed: **attack** `(attacker id, target id)`,
including builders mining a resource. The decoder refuses unknown tags so that a new one is noticed.

`map_size` is repeated in every delta and is used as a cheap sync check.

## How it was found

The binary ships symbols: `nm -C engine/aicup2020 | grep Delta` lists `ModelDelta`, `EntityDelta`,
`PlayerDelta`, `StoredActionDelta`, `CollectionDelta<T>` with their `trans::Trans::read_from`
implementations; disassembling them shows the three `Vec::read_from` calls of a collection delta and
the field order. The rest was confirmed against the data (entity ids, types and positions of known
units; `results.json` scores at the last tick).

## JSON for the viewer (`replay.to_json`)

```
{map_size, fog_of_war, max_tick_count, tick_count, entity_types, properties,
 players: [[id, index, score, resource]], entities: [[id, player_id, type, x, y, health, active]],
 actions: {id: action},
 ticks: [{t, p: [players], a: [added entities], r: [removed ids], u: [updated entities],
          act: {id: action}, actr: [ids], ev: [[tag, a, b]]}]}
```

A 500-tick 4-player game is ~2 MB raw, ~0.4 MB gzipped; the web server caches `replay.json.gz` next to
`replay.dat`.
