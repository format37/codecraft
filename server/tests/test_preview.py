from pathlib import Path

from PIL import Image

from codewars import preview as P
from codewars import replay as R
from test_replay import make_replay


def test_render_size_and_scores():
    rep = R.parse(make_replay())
    img = P.render(rep, ["alice", "bob"], heading="Game #1 · Round1", footer="codewars.test")
    assert img.size == (P.WIDTH, P.HEIGHT)
    # a unit of player 2 (index 1, red) stands at (9, 9): the top-right cell of the 10x10 field (56 px cells)
    r, g, b = img.getpixel((35 + 9 * 56 + 28, 35 + 28))
    assert r > 200 and g < 100
    assert P.state_at(rep, 1)[0] == 1 and P.state_at(rep, 99)[0] == 2 and P.state_at(rep)[0] == 2


def test_ensure_caches_and_rounds_ticks(tmp_path: Path):
    gdir = tmp_path / "000007"
    gdir.mkdir()
    (gdir / "replay.dat").write_bytes(make_replay())
    p = P.ensure(gdir, ["a", "b"], 7, "Round1")
    assert p == gdir / "preview.png" and Image.open(p).size == (1200, 630)
    m = p.stat().st_mtime_ns
    assert P.ensure(gdir, ["a", "b"], 7, "Round1").stat().st_mtime_ns == m      # cached
    assert P.ensure(gdir, ["a", "b"], 7, "Round1", tick=500) == gdir / "preview.png"   # past the end
    assert P.ensure(gdir, ["a", "b"], 7, "Round1", tick=1).name == "preview-0.png"     # rounded down
    assert P.ensure(gdir, ["a", "b"], 7, "Round1", tick=24).name == "preview-0.png"
