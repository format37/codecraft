"""The hub and its executors (hub.py, remote.py): the executors' API on a real database through the web app, the
leases, the GPU queue, the replays the hub lets go and fetches back, the banner of an offline game server."""
import json
import lzma
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from codecraft import hub as H
from codecraft import remote
from codecraft.auth import Auth
from codecraft.config import Config
from codecraft.db import DB, LeaseLost
from codecraft.web import create_app
from codecraft.worker import Worker

from test_web import REPO, FakeMailer

TOKEN = "s3cret-hub-token"


def make(tmp_path):
    (tmp_path / "docs").mkdir(exist_ok=True)
    for name in ("arena.md", "doc-en.md", "QUICKSTART.md"):
        shutil.copy(REPO / "docs" / name, tmp_path / "docs" / name)
    cfg = Config(Config.load(tmp_path).data, tmp_path)
    cfg.data["auth"]["base_url"] = "http://testserver"
    cfg.data["auth"]["cookie_secure"] = False
    cfg.data["hub"]["token"] = TOKEN
    cfg.resolve_gpu(None)
    cfg.ensure_dirs()
    db = DB(tmp_path / "t.db")
    seed = lambda name, builtin=None, gpu=False: SimpleNamespace(
        name=name, title=name, language=None if builtin else "python", gpu=gpu, builtin=builtin, enabled=True)
    db.upsert_strategy(seed("quickstart", "QuickStart"), 1200)
    db.upsert_strategy(seed("a"), 1200)
    db.set_image("a", "codecraft/strategy-a:1", "1")
    db.upsert_strategy(seed("g", gpu=True), 1200)
    db.set_image("g", "codecraft/strategy-g:1", "1")
    client = TestClient(create_app(cfg, db, Auth(cfg, db, FakeMailer())), follow_redirects=False)
    return cfg, db, client


def executor(cfg, client, name="node"):
    """The executor's side, talking to the app in-process."""
    ecfg = Config(json.loads(json.dumps(cfg.data)), cfg.root)
    ecfg.data["hub"]["url"] = "http://testserver"
    ecfg.data["executor"]["name"] = name
    hub = remote.Hub(ecfg, client=client, patience=0)
    return hub, remote.RemoteDB(hub)


def custom_game(db, strategy="a", gpu_version=False):
    vid = None
    if gpu_version:
        vid = db.create_version("g", None, "git", "https://github.com/x/y", None, "python", True, False, None, None)
    return db.queue_game("Finals", 7, None, [{"slot": 0, "strategy": strategy, "image": f"codecraft/strategy-{strategy}:1",
                                               "version_id": vid},
                                              {"slot": 1, "strategy": "quickstart"}], kind="custom")


def seats(score0=10, score1=5):
    return [dict(slot=0, strategy="a", image="codecraft/strategy-a:1", score=score0, crashed=False, comment=None,
                 cpu_sec=1.0, version_id=None, label=None),
            dict(slot=1, strategy="quickstart", image=None, score=score1, crashed=False, comment=None, cpu_sec=None,
                 version_id=None, label=None)]


def test_the_api_wants_the_token_and_opens_only_the_workers_methods(tmp_path):
    cfg, db, client = make(tmp_path)
    r = client.post("/internal/rpc", json={"method": "strategies"}, headers={"X-CW-Executor": "node"})
    assert r.status_code == 401
    r = client.post("/internal/rpc", json={"method": "strategies"},
                    headers={"Authorization": "Bearer wrong", "X-CW-Executor": "node"})
    assert r.status_code == 401
    hub, rdb = executor(cfg, client)
    assert {s["name"] for s in rdb.strategies()} == {"quickstart", "a", "g"}
    assert rdb.strategy("a")["image"] == "codecraft/strategy-a:1"          # a row reads as one
    with pytest.raises(remote.HubError, match="403"):
        rdb.users()                                                          # not the executors' business
    with pytest.raises(remote.HubError, match="403"):
        rdb.q("DELETE FROM users")
    # without a token configured there is no API at all
    cfg2 = Config(json.loads(json.dumps(cfg.data)), tmp_path)
    cfg2.data["hub"]["token"] = None
    plain = TestClient(create_app(cfg2, db, Auth(cfg2, db, FakeMailer())))
    assert plain.post("/internal/rpc", json={"method": "strategies"}).status_code == 404


def test_an_executor_plays_a_queued_game_and_its_files_reach_the_site(tmp_path):
    cfg, db, client = make(tmp_path)
    hub, rdb = executor(cfg, client)
    gid = custom_game(db)
    game = rdb.next_queued_game(gpu=False)
    assert game["id"] == gid
    assert rdb.claim_game(gid, "somebody-else")                    # the hub writes the caller's name, not this
    assert db.game(gid)[0]["executor"] == "node"
    gdir = tmp_path / "node-games" / f"{gid:06d}"
    gdir.mkdir(parents=True)
    from test_replay import make_battle_replay
    (gdir / "replay.dat").write_bytes(make_battle_replay())
    (gdir / "engine.log").write_text("engine ok")
    (gdir / "player-0.log").write_text("my output")
    remote.upload_results(hub, gid, gdir)
    assert not (gdir / "replay.dat").exists() and (gdir / "replay.dat.xz").is_file()     # kept, compressed
    rows = rdb.finish_game(gid, "finished", None, 3.0, 7, seats(), {"initial": 1}, [2, 0], rated=False)
    assert [r["place"] for r in rows] == [1, 2]
    (gdir / "meta.json").write_text("{}")
    remote.upload_final(hub, gid, gdir)
    sdir = cfg.games_dir / f"{gid:06d}"
    assert sorted(p.name for p in sdir.iterdir()) == ["engine.log", "meta.json", "player-0.log", "replay.dat.xz"]
    rep = client.get(f"/games/{gid}/replay.json")
    assert rep.status_code == 200 and rep.json()["ticks"][0]["t"] == 1
    page = client.get(f"/games/{gid}").text
    assert f'/games/{gid}/files/replay.dat"' in page and "replay.dat.xz" not in page
    raw = client.get(f"/games/{gid}/files/replay.dat")
    assert raw.status_code == 200 and raw.content == make_battle_replay()
    assert client.get(f"/games/{gid}/viewer").status_code == 200
    # a game file is only the game's executor's to write, and only the files a game has
    other, _ = executor(cfg, client, "gpu-box")
    with pytest.raises(remote.HubError, match="409"):
        other.put_game_file(gid, "engine.log", b"x")
    with pytest.raises(remote.HubError, match="400"):
        hub.put_game_file(gid, "codecraft.db", b"x")


def test_a_result_from_an_executor_that_lost_the_game_is_refused(tmp_path):
    cfg, db, client = make(tmp_path)
    hub, rdb = executor(cfg, client)
    _, other = executor(cfg, client, "gpu-box")
    gid = custom_game(db)
    assert rdb.claim_game(gid)
    with pytest.raises(LeaseLost):
        other.finish_game(gid, "finished", None, 1.0, 7, seats(), {}, [2, 0], rated=False)
    other.mark_error(gid, "not mine")                                       # a no-op for anybody else
    assert db.game(gid)[0]["status"] == "running"
    # the hub hears nothing from node for longer than the lease: the game goes back to the queue
    hub.heartbeat({"gpu": False})
    with db.tx() as c:
        c.execute("UPDATE executors SET last_seen=datetime('now', '-1 hour')")
    H.HubLoop(cfg, db, send=lambda t: True).expire()
    row = db.game(gid)[0]
    assert row["status"] == "queued" and row["executor"] is None
    with pytest.raises(LeaseLost):                                          # node comes back with a late result
        rdb.finish_game(gid, "finished", None, 1.0, 7, seats(), {}, [2, 0], rated=False)


def test_a_restart_gives_back_only_that_machines_work(tmp_path):
    cfg, db, client = make(tmp_path)
    _, node = executor(cfg, client)
    _, box = executor(cfg, client, "gpu-box")
    g1, g2 = custom_game(db), custom_game(db)
    assert node.claim_game(g1) and box.claim_game(g2)
    sandbox = node.create_game("Finals", 1, "sandbox")                     # made up on the spot: no seats stored
    v = db.create_version("a", None, "git", "https://github.com/x/y", None, "python", False, False, None, None)
    assert box.start_version(v) and not node.start_version(v)              # taken once
    assert node.abort_stale_running() == 2 and node.abort_stale_builds() == 0
    assert db.game(g1)[0]["status"] == "queued" and db.game(g2)[0]["status"] == "running"
    assert db.game(sandbox)[0]["status"] == "error"
    assert box.abort_stale_builds() == 1 and db.version(v)["status"] == "queued"


def test_gpu_work_waits_for_a_gpu_machine_and_the_operator_hears_of_it(tmp_path):
    cfg, db, client = make(tmp_path)
    hub, node = executor(cfg, client)
    gid = custom_game(db, "g", gpu_version=True)              # its version (1) waits for a build, a GPU one
    plain = custom_game(db)
    v = 1
    assert node.next_queued_game(gpu=False)["id"] == plain
    assert node.next_queued_game(gpu=True)["id"] == gid
    assert node.next_queued_version(gpu=False) is None                       # a GPU build needs a GPU machine
    assert node.next_queued_version(gpu=True, gpu_only=True)["id"] == v
    sent = []
    loop = H.HubLoop(cfg, db, send=lambda text: sent.append(text) or True)
    hub.heartbeat({"gpu": False})
    loop.gpu_alert()
    assert sent == []                                                        # not yet: it waits only a moment
    with db.tx() as c:
        c.execute("UPDATE strategy_versions SET created_at=datetime('now', '-1 hour')")
        c.execute("UPDATE games SET created_at=datetime('now', '-1 hour')")
    loop.gpu_alert()
    loop.gpu_alert()
    assert len(sent) == 1 and "g v" in sent[0] and f"game #{gid}" in sent[0]  # once, not every half minute
    box, _ = executor(cfg, client, "gpu-box")
    box.heartbeat({"gpu": True})
    loop.gpu_alert()
    assert db.meta("gpu_alert_at") is None                                   # a GPU is online: the alert is over


def test_the_hub_lets_old_replays_go_and_fetches_them_back(tmp_path):
    cfg, db, client = make(tmp_path)
    hub, node = executor(cfg, client)
    gid = custom_game(db)
    assert node.claim_game(gid)
    from test_replay import make_battle_replay
    gdir = cfg.games_dir / f"{gid:06d}"
    gdir.mkdir(parents=True)
    xz = lzma.compress(make_battle_replay())
    (gdir / "replay.dat.xz").write_bytes(xz)
    node.finish_game(gid, "finished", None, 3.0, 7, seats(), {}, [2, 0], rated=False)
    loop = H.HubLoop(cfg, db, send=lambda t: True)
    assert loop.sweep(force=True) == 0                                       # a fresh one stays
    old = 40 * 86400
    with db.tx() as c:
        c.execute("UPDATE games SET finished_at=datetime('now', '-40 days')")
    import os, time
    os.utime(gdir / "replay.dat.xz", (time.time() - old, time.time() - old))
    assert loop.sweep(force=True) == 1 and not (gdir / "replay.dat.xz").exists()
    # somebody opens it: the hub asks the executor, which answers with its next heartbeat
    r = client.get(f"/games/{gid}/replay.json")
    assert r.status_code == 503 and "being fetched" in r.json()["detail"]
    assert client.get(f"/games/{gid}/viewer").status_code == 503
    assert hub.heartbeat({"gpu": False}) == {"restore": [gid]}
    ndir = tmp_path / "node" / f"{gid:06d}"
    ndir.mkdir(parents=True)
    (ndir / "replay.dat.xz").write_bytes(xz)
    w = object.__new__(Worker)
    w.cfg, w.hub, w.db = SimpleNamespace(games_dir=tmp_path / "node"), hub, node
    w.restore(gid)
    assert hub.heartbeat({"gpu": False}) == {"restore": []}
    assert client.get(f"/games/{gid}/replay.json").status_code == 200
    assert loop.sweep(force=True) == 0                                       # fetched back: it stays a while


def test_the_site_says_when_the_game_server_is_offline(tmp_path):
    cfg, db, client = make(tmp_path)
    hub, _ = executor(cfg, client)
    custom_game(db)
    page = client.get("/leaderboard").text
    assert "The game server is offline" in page                              # nobody has called yet
    hub.heartbeat({"gpu": False})
    assert "The game server is offline" not in client.get("/leaderboard").text
    hub.heartbeat({"gpu": False, "stopping": True})                          # stopping for a reboot
    page = client.get("/leaderboard").text
    assert "The game server is offline" in page and "1 game now" in page
    # on one machine there is no hub and no banner
    cfg2 = Config(json.loads(json.dumps(cfg.data)), tmp_path)
    cfg2.data["hub"]["token"] = None
    plain = TestClient(create_app(cfg2, db, Auth(cfg2, db, FakeMailer())))
    assert "The game server is offline" not in plain.get("/leaderboard").text


def test_an_executor_builds_with_the_sources_and_the_log_going_through_the_hub(tmp_path):
    cfg, db, client = make(tmp_path)
    hub, node = executor(cfg, client)
    from codecraft.submissions import submit
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("my_strategy.py", "print('hi')\n")
    (tmp_path / "templates" / "python").mkdir(parents=True)
    (tmp_path / "templates" / "python" / "Dockerfile").write_text("FROM python\n")
    user = db.create_user("u@example.com", "U")
    vid = submit(cfg, db, name="mine", language="python", user=dict(user), zip_data=buf.getvalue())
    ecfg = hub.cfg
    ecfg.root = tmp_path / "node"
    ecfg.ensure_dirs()

    class FakeBuilder:
        logs_dir = ecfg.build_dir / "logs"
        last_commit = None

        def build_strategy(self, strategy, **kw):
            assert (Path(strategy.source.dir) / "my_strategy.py").read_text() == "print('hi')\n"
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            (self.logs_dir / "mine-v1.log").write_text("compiled fine\n")
            return "codecraft/strategy-mine:abc", "abc"

    w = object.__new__(Worker)
    w.cfg, w.db, w.hub, w.name, w.gpu, w.builds = ecfg, node, hub, "node", False, True
    w.builder, w.building = FakeBuilder(), None
    w.announcer = SimpleNamespace(built=lambda *a: None)
    assert w.build_one() is True
    v = db.version(vid)
    assert v["status"] == "ok" and v["executor"] == "node" and v["image"] == "codecraft/strategy-mine:abc"
    assert (cfg.data_dir / v["log_path"]).read_text() == "compiled fine\n"   # the hub has the log
    assert db.strategy("mine")["active_version"] == vid
