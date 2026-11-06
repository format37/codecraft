"""The replay reader and writer against the official files in data/games (the free corpus) and the server's own
decoder. SIM_SLOW=1 runs the round trip over every stored replay (about 2 minutes for 136 games)."""
from __future__ import annotations

import pytest

from codecraft import replay as server_replay
from codecraft_sim import replay as replay_io
from codecraft_sim.model import Event
from codecraft_sim.trans import ProtocolError

from conftest import SLOW, needs_corpus, replays


def sample() -> list:
    paths = replays()
    if SLOW or len(paths) <= 4:
        return paths
    by_size = sorted(paths, key=lambda p: p.stat().st_size)
    return [by_size[0], by_size[len(by_size) // 2], by_size[-1], paths[-1]]


@needs_corpus
@pytest.mark.parametrize("path", sample(), ids=lambda p: p.parent.name)
def test_round_trip_is_byte_exact(path):
    data = path.read_bytes()
    assert replay_io.write_replay(replay_io.parse(data)) == data


@needs_corpus
def test_states_agree_with_the_server_decoder():
    path = sample()[0]
    ours = list(replay_io.load(path).states())
    theirs = server_replay.load(path)
    assert len(ours) == len(theirs.ticks) + 1
    for m, (tick, players, entities, actions) in zip(ours, theirs.states()):
        assert m.current_tick == tick
        assert {i: [p.id, p.index, p.score, p.resource] for i, p in m.players.items()} == players
        assert {i: [e.id, e.player_id, int(e.entity_type), e.position.x, e.position.y, e.health, e.active]
                for i, e in m.entities.items()} == entities
        assert set(m.entity_actions) == set(actions)


@needs_corpus
def test_diff_finds_what_the_official_deltas_hold():
    """The official delta lists exactly the changed items (checked over whole games when this was written): our
    diff gives the same sets, in its own order."""
    rep = replay_io.load(sample()[1])
    states = list(rep.states())
    for prev, cur, t in zip(states, states[1:], rep.ticks):
        d, o = replay_io.diff(prev, cur), t.delta
        assert {p.id for p in d.players.added} == {p.id for p in o.players.added}
        assert {k for k, _ in d.players.updated} == {k for k, _ in o.players.updated}
        assert {e.id for e in d.entities.added} == {e.id for e in o.entities.added}
        assert set(d.entities.removed) == set(o.entities.removed)
        assert {k for k, _ in d.entities.updated} == {k for k, _ in o.entities.updated}
        assert {k for k, _ in d.entity_actions.added} == {k for k, _ in o.entity_actions.added}
        assert {k for k, _ in d.entity_actions.updated} == {k for k, _ in o.entity_actions.updated}
        assert not o.entity_actions.removed          # the official engine never drops a stored action


@needs_corpus
def test_replay_writer_output_reads_back_in_the_server_decoder(tmp_path):
    """A game re-written tick by tick with ReplayWriter (diffs computed by us, items in our order) decodes in the
    server's decoder to the same states and final scores."""
    rep = replay_io.load(sample()[0])
    states = list(rep.states())
    out = tmp_path / "replay.dat"
    with replay_io.ReplayWriter(out) as w:
        w.start(states[0])
        for m, t in zip(states[1:], rep.ticks):
            w.tick(t.events, m)
    theirs = server_replay.load(out)
    assert server_replay.final_scores(theirs) == server_replay.final_scores(server_replay.load(sample()[0]))
    assert replay_io.load(out).tick_count == rep.tick_count


def test_writer_and_reader_on_a_hand_made_game():
    from oracle.state import State, attack
    s = State(players=2, ticks=3)
    a = s.add("MeleeUnit", 1, (1, 1))
    b = s.add("BuilderUnit", 2, (2, 1))
    s.order(a, attack(b))
    m0 = s.model
    m1 = m0.copy()
    m1.current_tick = 1
    m1.entities[b].health = 5
    m2 = m1.copy()
    m2.current_tick = 2
    del m2.entities[b]
    m2.players[1].score = 100
    rep = replay_io.Replay(m0, [replay_io.TickRecord([Event(0, a, b)], replay_io.diff(m0, m1)),
                                replay_io.TickRecord([Event(0, a, b)], replay_io.diff(m1, m2))])
    data = replay_io.write_replay(rep)
    back = replay_io.parse(data)
    assert [m.entities for m in back.states()] == [m0.entities, m1.entities, m2.entities]
    assert server_replay.final_scores(server_replay.parse(data)) == [100, 0]


def test_reader_refuses_garbage():
    with pytest.raises(ProtocolError, match="bad bool 7"):
        replay_io.parse(b"\x05\x00\x00\x00\x14\x00\x00\x00\x07")       # next_id, map_size, then fog_of_war = 7
    with pytest.raises(ProtocolError, match="truncated"):
        replay_io.parse(replay_io.write_replay(replay_io.parse(sample_bytes()))[:-5])


def sample_bytes() -> bytes:
    from oracle.state import State
    s = State(players=2, ticks=1)
    s.add("BuilderUnit", 1, (1, 1))
    m1 = s.model.copy()
    m1.current_tick = 1
    return replay_io.write_replay(replay_io.Replay(s.model, [replay_io.TickRecord([], replay_io.diff(s.model, m1))]))
