"""Probe the official replay format (work in progress, see docs/replay-format.md).
Usage: python3 server/tools/replay_probe.py data/games/000015/replay.dat"""
import io, sys, struct
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "clients" / "Python"))
from stream_wrapper import StreamWrapper
from model.entity_properties import EntityProperties
from model.player import Player
from model.entity import Entity

path = sys.argv[1]
data = open(path, "rb").read()
buf = io.BytesIO(data)
r = StreamWrapper(buf)
def pos(): return buf.tell()

next_id = r.read_int(); map_size = r.read_int(); fog = r.read_bool()
print(f"next_id={next_id} map_size={map_size} fog={fog}")
n = r.read_int(); props = {}
for _ in range(n):
    k = r.read_int(); props[k] = EntityProperties.read_from(r)
print(f"entity_properties: {n} types, offset now {pos()}")
a, b, c, d = r.read_int(), r.read_int(), r.read_int(), r.read_int()
print(f"next 4 ints: {a} {b} {c} {d}  (expect max_tick_count=1000, max_pathfind_nodes, current_tick=0, passive_income?)")
np_ = r.read_int(); print(f"players: {np_}")
players = [Player.read_from(r) for _ in range(np_)]
print("  ", [(p.id, p.score, p.resource) for p in players])
ne = r.read_int(); print(f"entities: {ne} @ {pos()}")
ents = [Entity.read_from(r) for _ in range(ne)]
from collections import Counter
print("  types:", Counter(e.entity_type for e in ents).most_common(), " ids:", min(e.id for e in ents), "..", max(e.id for e in ents))
print(f"after entities offset {pos()} / {len(data)}")
nxt = [struct.unpack_from('<i', data, pos() + 4*i)[0] for i in range(12)]
print("next 12 ints:", nxt)
print("next 48 bytes:", data[pos():pos()+48].hex(' '))
