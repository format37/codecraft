# Fixtures

Each file is one rule question put to the official engine (`engine/aicup2020` 1.3.0) and its answer, as
language-neutral JSON. The Python engine and the C++ clone must both reproduce them. They are recorded, never
edited by hand: the scenario that makes a fixture lives in `sim/oracle/scenarios.py`, and

```
cd sim
python3 -m oracle.scenarios record NAME     # (re)write fixtures/NAME.json from the official engine
python3 -m oracle.scenarios check           # run every scenario again and compare with its file
python3 -m oracle.scenarios show NAME       # the fixture tick by tick, as a human reads it
```

## Format

```json
{
 "name": "attack-melee",
 "about": "what the fixture shows, in one paragraph",
 "questions": [4],
 "engine": "aicup2020 1.3.0",
 "seed": 1,
 "timeout": 1.0, "accept_timeout": 5.0,
 "players": ["Empty", {"puppet": {"script": {"0": {"3": <EntityAction>}}, "crash_at": 2}}],
 "random": "what another engine may decide differently",
 "state": <Model>,
 "expected": {
  "returncode": 0,
  "results": <results.json> | null,
  "log": ["ERROR - Got action for entity not belonging to the player"],
  "puppets": {"0": {"view_ticks": [0, 1, 2], "finished": true, "views": [<PlayerView>, ...]}},
  "ticks": [<Snapshot>, ...]
 }
}
```

- `questions`: the rule questions the fixture answers (numbered as in "Rule questions" below).
- `timeout`, `accept_timeout` (seconds) are there only when a seat is a puppet.
- `players`, one per seat: `"Empty"` (never answers; the stored actions drive its entities) or a puppet, a
  scripted TCP player. `script` maps the `current_tick` of a view to the Action the puppet answers it with (entity
  id -> EntityAction, in the seat's own frame; a tick that is not listed is answered with an empty Action).
  Misbehaviour: `crash_at` (close the connection instead of answering that view), `sleep_at` (tick -> seconds to
  wait before answering), `garbage_at` (answer with an unknown message tag), `token` (send this instead of the
  configured token), `connect: false` (never connect).
- `random`: present when the outcome depends on the engine's random numbers (attack order, build order, movement
  conflicts, the choice of an auto-attack target). The recording is the outcome of one seed of the official
  engine: another engine is right if it ends in one of the outcomes the rules allow, which the field names.
- `state`: the starting `Model` in the engine's serde JSON, as fed to `{"LoadFrom": ...}`, except that
  `"entity_properties": "default"` stands for the table of `docs/doc-en.md` (`DEFAULT_PROPERTIES` in
  `codecraft_sim/model.py`). Collections are `{"by_id": {"<id>": value}}`; enum values are strings; the attack
  range is `range` (`attack_range` on the wire).
- `expected.results`: the engine's `results.json` (null when it wrote none: a game already over at the start).
- `expected.log`: the engine's log lines worth comparing (colour codes and port bookkeeping removed).
- `expected.puppets`: per puppet seat, the `current_tick` of every view it got, whether the server sent `Finish`,
  and with `record_views` the views themselves (`my_id`, the scalars, `players` and `entities` as received:
  positions are in the seat's frame).
- `expected.ticks`: one snapshot per state, from the starting one (tick = the state's `current_tick`) to the last:
  `{"tick", "next_id", "players": [...], "entities": [...], "entity_actions": [{"entity_id", "action"}],
  "events": [[0, attacker, target], ...]}`, players, entities and actions sorted by id. `events` are those of the
  tick that produced the snapshot, in the engine's processing order.

## Rule questions

1. The order of the phases in a tick, and what is random.
2. When the Action answered to a view shows up in the replay's stored actions.
3. Distances when an entity is bigger than one cell (attack range, sight, build sites).
4. Attacks: explicit, auto-attack, friendly fire, overkill, what scores.
5. Movement: the path, occupied and unreachable targets, `find_closest_position`, `break_through`, conflicts.
6. Building: placement, cost, population, activation.
7. Repair.
8. Fog of war and the per-seat view.
9. Crashed seats.
10. `passive_income`.
11. Resource exhaustion.
12. The end of the game and the survivor bonus.

## Reading a fixture against another engine

Load `state`, play the `players` (a puppet's script becomes that seat's answers), run until the game ends, and
compare every snapshot. Where `random` is absent everything must match exactly. Where it is present, compare
what the rules fix (totals, who could have done what) rather than the exact list.
