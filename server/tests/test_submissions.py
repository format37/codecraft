import io
import zipfile

import pytest

from codewars.config import Config
from codewars.db import DB
from codewars.submissions import SubmissionError, check_name, extract_zip, submit


def zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_extract_unwraps_single_directory_and_refuses_escapes(tmp_path):
    n = extract_zip(zip_bytes({"bot/my_strategy.py": "x", "bot/lib/a.py": "y", "__MACOSX/._junk": ""}),
                    tmp_path / "src", 100, 10)
    assert n == 2
    assert (tmp_path / "src" / "my_strategy.py").read_text() == "x"
    assert (tmp_path / "src" / "lib" / "a.py").read_text() == "y"
    for bad in ("../x.py", "/etc/passwd", "a/../../b"):
        with pytest.raises(SubmissionError):
            extract_zip(zip_bytes({bad: "x"}), tmp_path / "bad", 100, 10)
    with pytest.raises(SubmissionError):
        extract_zip(b"not a zip", tmp_path / "bad2", 100, 10)
    with pytest.raises(SubmissionError):
        extract_zip(zip_bytes({f"f{i}.py": "x" for i in range(5)}), tmp_path / "bad3", 3, 10)


def test_names():
    assert check_name(" My-Bot ") == "my-bot"
    for bad in ("", "-x", "Empty", "a" * 50, "new", "a b"):
        with pytest.raises(SubmissionError):
            check_name(bad)


def make_root(tmp_path):
    (tmp_path / "clients" / "Python").mkdir(parents=True)
    (tmp_path / "clients" / "Python" / "Dockerfile").write_text("FROM python:3.8\n")
    (tmp_path / "templates").mkdir()
    cfg = Config(Config.load(tmp_path).data, tmp_path)
    cfg.ensure_dirs()
    return cfg, DB(tmp_path / "data" / "t.db")


def test_submit_zip_creates_strategy_version_and_quota(tmp_path):
    cfg, db = make_root(tmp_path)
    user = dict(db.create_user("u@b.co", "U"))
    vid = submit(cfg, db, name="bot1", language="python", user=user, zip_data=zip_bytes({"my_strategy.py": "x"}))
    v = db.version(vid)
    assert v["status"] == "queued" and v["number"] == 1 and v["source_kind"] == "zip"
    assert (cfg.data_dir / v["source"] / "my_strategy.py").read_text() == "x"
    assert db.strategy("bot1")["owner_id"] == user["id"] and db.strategy("bot1")["kind"] == "user"
    with pytest.raises(SubmissionError):  # one pending version per user
        submit(cfg, db, name="bot1", language="python", user=user, zip_data=zip_bytes({"my_strategy.py": "y"}))
    db.finish_version(vid, "ok", image="img:1", build_hash="1")
    assert db.strategy("bot1")["image"] == "img:1" and db.strategy("bot1")["active_version"] == vid
    vid2 = submit(cfg, db, name="bot1", language="python", user=user, git_url="https://github.com/x/y", git_ref="main")
    assert db.version(vid2)["number"] == 2 and db.version(vid2)["source"] == "https://github.com/x/y"
    other = dict(db.create_user("o@b.co", "O"))
    with pytest.raises(SubmissionError):  # not the owner
        submit(cfg, db, name="bot1", language="python", user=other, zip_data=zip_bytes({"a.py": ""}))
    with pytest.raises(SubmissionError):
        submit(cfg, db, name="bot2", language="cobol", user=other, zip_data=zip_bytes({"a.py": ""}))
    with pytest.raises(SubmissionError):
        submit(cfg, db, name="bot2", language="python", user=other, git_url="file:///etc")


def test_queued_custom_game_round_trip(tmp_path):
    cfg, db = make_root(tmp_path)
    user = dict(db.create_user("u@b.co", "U"))
    gid = db.queue_game("Finals", 7, user["id"], [
        {"slot": 0, "strategy": "bot1", "version_id": None, "image": "img:1", "label": "bot1"},
        {"slot": 1, "strategy": None},
    ])
    g = db.next_queued_game()
    assert g["id"] == gid and g["status"] == "queued" and g["kind"] == "custom" and g["rated"] == 0
    assert db.claim_game(gid) and not db.claim_game(gid)
    assert db.user_pending_games(user["id"]) == 1
    _, players = db.game(gid)
    assert players[0]["label"] == "bot1" and players[1]["strategy"] is None


def test_bad_archive_leaves_no_strategy_behind(tmp_path):
    cfg, db = make_root(tmp_path)
    user = dict(db.create_user("u@b.co", "U"))
    with pytest.raises(SubmissionError):
        submit(cfg, db, name="evil", language="python", user=user, zip_data=zip_bytes({"../x.py": "x"}))
    assert db.strategy("evil") is None and db.versions("evil") == []
    assert db.leaderboard() == []   # nothing built yet -> nothing on the ladder


def test_gpu_languages_follow_resolved_availability(tmp_path):
    from codewars.config import Config
    (tmp_path / "templates" / "python-cuda").mkdir(parents=True)
    (tmp_path / "templates" / "python-cuda" / "Dockerfile").write_text("FROM x\n")
    (tmp_path / "templates" / "python").mkdir()
    (tmp_path / "templates" / "python" / "Dockerfile").write_text("FROM x\n")
    cfg = Config.load(tmp_path)
    assert cfg.gpu_ok                      # 'auto' counts as available until resolved
    assert "python-cuda" in cfg.languages()
    assert cfg.resolve_gpu(None) is False  # auto without docker -> no GPU
    assert cfg.languages() == ["python"]
    assert cfg.languages(gpu=True) == ["python", "python-cuda"]
    cfg.data["gpu"]["enabled"] = True
    assert cfg.resolve_gpu(None) is True


def test_env_overrides_registration_gpu_and_secure_cookie(tmp_path, monkeypatch):
    from codewars.config import Config
    monkeypatch.setenv("CW_REGISTRATION", "Invite")
    monkeypatch.setenv("CW_GPU_ENABLED", "false")
    monkeypatch.setenv("CW_BASE_URL", "https://codewars.example")
    cfg = Config.load(tmp_path)
    assert cfg["auth"]["registration"] == "invite"
    assert cfg["gpu"]["enabled"] is False and cfg.gpu_ok is False
    assert cfg["auth"]["cookie_secure"] is True
    monkeypatch.setenv("CW_BASE_URL", "http://localhost:8080")
    assert Config.load(tmp_path)["auth"]["cookie_secure"] is False
