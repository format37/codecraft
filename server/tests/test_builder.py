import io
import subprocess
import tarfile
import threading
from pathlib import Path
from unittest.mock import MagicMock

import docker
import pytest

from codecraft import builder as builder_module
from codecraft.builder import (Builder, BuildError, _CappedLog, _tar_sources, check_git_url, compile_command,
                               git_failure, language_subdir)
from codecraft.config import Config
from codecraft.strategies import load_strategy

HOSTS = ["github.com", "gitlab.com", "bitbucket.org", "codeberg.org"]


def make_root(tmp_path: Path) -> Path:
    (tmp_path / "clients" / "Python").mkdir(parents=True)
    (tmp_path / "clients" / "Python" / "Dockerfile").write_text("FROM python:3.8\nCOPY . /project\nWORKDIR /project\n")
    (tmp_path / "clients" / "Python" / "compile.sh").write_text("cp -r * /output/\n")
    (tmp_path / "clients" / "Python" / "run.sh").write_text("python main.py\n")
    (tmp_path / "clients" / "Python" / "my_strategy.py").write_text("stock\n")
    (tmp_path / "templates").mkdir()
    (tmp_path / "data").mkdir()
    return tmp_path


def seed(root: Path, name: str, source: str, extra: str = "") -> Path:
    sdir = root / "strategies" / name
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "strategy.yaml").write_text(f"name: {name}\nlanguage: python\n{extra}source:\n{source}")
    return sdir


def tar_members(data: bytes) -> dict[str, bytes | None]:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return {m.name: (tar.extractfile(m).read() if m.isfile() else None) for m in tar.getmembers()}


# Sources, the base image, the compile command (security review, finding 2) ----------------------------------
def test_sources_from_a_local_dir_with_include_overlay_and_prebuild(tmp_path):
    root = make_root(tmp_path)
    sdir = seed(root, "s1", "  dir: src\n  include: [my_strategy.py]\n", "overlay: overlay\nprebuild: pre.sh\n")
    (sdir / "src").mkdir()
    (sdir / "src" / "my_strategy.py").write_text("mine\n")
    (sdir / "src" / "README.md").write_text("ignored\n")
    (sdir / "overlay").mkdir()
    (sdir / "overlay" / "extra.txt").write_text("overlay\n")
    (sdir / "pre.sh").write_text("echo hi\n")
    builder = Builder(Config(Config.load(root).data, root), client=None)
    strategy = load_strategy(sdir / "strategy.yaml")
    builder._assemble_sources(strategy, tmp_path / "src")
    assert sorted(p.name for p in (tmp_path / "src").iterdir()) == ["extra.txt", "my_strategy.py"]
    prebuild = builder._prebuild_text(strategy)
    assert prebuild == "echo hi\n"
    buf = io.BytesIO()
    _tar_sources(tmp_path / "src", "src", prebuild, buf)
    assert tar_members(buf.getvalue()) == {"src/extra.txt": b"overlay\n", "src/my_strategy.py": b"mine\n",
                                           "cw-prebuild.sh": b"echo hi\n"}
    assert compile_command(merge=False, prebuild=True) == (
        "cd /src && bash /cw-prebuild.sh && cp /project/run.sh /cw-run.sh && cd /project && bash compile.sh")


def test_merge_mode_puts_the_files_on_top_of_the_client_and_skips_the_copy(tmp_path):
    root = make_root(tmp_path)
    sdir = seed(root, "s3", "  dir: src\n  merge: true\n", "limits: {memory_mb: 2048}\n")
    (sdir / "src" / "lib").mkdir(parents=True)
    (sdir / "src" / "lib" / "kernel.cu").write_text("__global__ void k() {}\n")
    (sdir / "src" / "run.sh").write_text("python main.py\n")
    (sdir / "src" / "run.sh").chmod(0o755)
    strategy = load_strategy(sdir / "strategy.yaml")
    assert strategy.source.merge and strategy.limits == {"memory_mb": 2048}
    builder = Builder(Config(Config.load(root).data, root), client=None)
    builder._assemble_sources(strategy, tmp_path / "src")
    buf = io.BytesIO()
    _tar_sources(tmp_path / "src", "project", None, buf)
    with tarfile.open(fileobj=io.BytesIO(buf.getvalue())) as tar:
        members = {m.name: m for m in tar.getmembers()}
    assert sorted(members) == ["project/lib", "project/lib/kernel.cu", "project/run.sh"]
    assert members["project/lib"].isdir() and members["project/run.sh"].mode == 0o755
    assert members["project/lib/kernel.cu"].mode == 0o644 and members["project/run.sh"].uid == 0
    assert compile_command(merge=True, prebuild=False) == "cp /project/run.sh /cw-run.sh && cd /project && bash compile.sh base"


def test_the_base_dockerfile_copies_the_client_and_prepares_an_offline_compile(tmp_path):
    root = make_root(tmp_path)
    builder = Builder(Config(Config.load(root).data, root), client=None)
    text, client_dir = builder.base_dockerfile("python")
    assert client_dir.name == "Python"
    assert "COPY client /project" in text and "COPY . /project" not in text
    assert text.rstrip().endswith("RUN mkdir -p /output /src") and ".m2" not in text
    image = builder.base_image("python")
    assert image.startswith("codecraft/base-python:") and len(image.split(":")[1]) == 12
    (client_dir / "pom.xml").write_text("<project/>\n")          # a Maven client: Maven stays offline
    text, _ = builder.base_dockerfile("python")
    assert text.rstrip().endswith("<settings><offline>true</offline></settings>' > /root/.m2/settings.xml")
    assert builder.base_image("python") != image                     # the client is part of the base's hash


def test_unknown_language_is_an_error(tmp_path):
    root = make_root(tmp_path)
    sdir = seed(root, "s2", "  dir: src\n")
    (sdir / "src").mkdir()
    (sdir / "src" / "x.rs").write_text("")
    (sdir / "strategy.yaml").write_text("name: s2\nlanguage: brainfuck\nsource:\n  dir: src\n")
    builder = Builder(Config(Config.load(root).data, root), client=None)
    with pytest.raises(BuildError, match="unknown language"):
        builder._assemble_sources(load_strategy(sdir / "strategy.yaml"), tmp_path / "src2")


def test_unknown_limit_key_is_rejected(tmp_path):
    sdir = tmp_path / "s4"
    (sdir / "src").mkdir(parents=True)
    (sdir / "strategy.yaml").write_text("name: s4\nlanguage: python\nlimits: {ram: 1}\nsource:\n  dir: src\n")
    with pytest.raises(ValueError):
        load_strategy(sdir / "strategy.yaml")


def test_a_multi_language_repository_yields_the_directory_of_the_chosen_language(tmp_path):
    repo = tmp_path / "starter"
    for d in ("python", "Cpp", "java-graalvm-native", ".github"):
        (repo / d).mkdir(parents=True)
        (repo / d / "x").write_text("src\n")
    for f in ("README.md", "LICENSE", ".gitignore", "CHANGELOG.txt"):
        (repo / f).write_text("doc\n")
    assert language_subdir(repo, "python") == repo / "python"
    assert language_subdir(repo, "cpp") == repo / "Cpp"                      # case does not matter
    assert language_subdir(repo, "python-pypy") == repo / "python"           # a variant falls back
    assert language_subdir(repo, "java-graalvm-native") == repo / "java-graalvm-native"
    assert language_subdir(repo, "rust") == repo                            # no such directory: as it is
    # a single-language project with a directory that happens to carry a language name is left alone
    (repo / "main.cpp").write_text("int main() {}\n")
    assert language_subdir(repo, "cpp") == repo


# The compile container, with a mocked Docker client -----------------------------------------------------------
def compile_env(tmp_path, timeout_sec=1200):
    root = make_root(tmp_path)
    cfg = Config(Config.load(root).data, root)
    cfg.data["build"]["timeout_sec"] = timeout_sec
    client = MagicMock()
    container = MagicMock()
    container.id = "c0ffee"
    container.attrs = {"State": {"OOMKilled": False}}
    container.logs.return_value = iter([b"+ cmake .\n", b"\x1b[91mwarn", b"ing\x1b[0m\n", b"done"])
    container.wait.return_value = {"StatusCode": 0}
    sent = {}

    def put_archive(path, data):
        sent["path"], sent["tar"] = path, data.read()
        return True

    container.put_archive.side_effect = put_archive
    client.containers.create.return_value = container
    src = tmp_path / "src"
    src.mkdir()
    (src / "my_strategy.py").write_text("mine\n")
    strategy = load_strategy(seed(root, "bot", "  dir: src\n") / "strategy.yaml")
    builder = Builder(cfg, client)
    out = _CappedLog(tmp_path / "bot.log", 2 ** 20)
    return builder, client, container, strategy, src, out, sent


def test_the_compile_container_is_offline_limited_and_committed(tmp_path):
    builder, client, container, strategy, src, out, sent = compile_env(tmp_path)
    builder._compile(strategy, "codecraft/base-python:b", src, None, "codecraft/strategy-bot:abc", out, 1200)
    out.close()
    args, kw = client.containers.create.call_args
    assert args == ("codecraft/base-python:b",)
    assert kw["network_mode"] == "none" and kw["cap_drop"] == ["ALL"] and kw["security_opt"] == ["no-new-privileges:true"]
    assert kw["mem_limit"] == kw["memswap_limit"] == "6144m" and kw["nano_cpus"] == 4 * 10 ** 9 and kw["pids_limit"] == 1024
    assert kw["log_config"]["Config"] == {"max-size": "16m", "max-file": "1"} and kw["labels"] == {"codecraft.game": "build-bot"}
    assert kw["entrypoint"] == ["bash", "-c", "cp /project/run.sh /cw-run.sh && cd /project && bash compile.sh"]
    assert kw["command"] == [] and kw["environment"] == {"HOME": "/root", "CW_BUILD": "bot"}
    assert sent["path"] == "/" and tar_members(sent["tar"]) == {"src/my_strategy.py": b"mine\n"}
    client.api.commit.assert_called_once_with("c0ffee", repository="codecraft/strategy-bot", tag="abc",
                                              changes=['ENTRYPOINT ["bash", "/cw-run.sh"]', "CMD []"])
    container.remove.assert_called_once_with(force=True)
    log = (tmp_path / "bot.log").read_text()
    assert "+ cmake .\nwarning\ndone\n" in log and "\x1b" not in log          # colours stripped, lines joined


def test_a_failed_compile_leaves_no_container_and_says_why(tmp_path):
    builder, client, container, strategy, src, out, sent = compile_env(tmp_path)
    container.wait.return_value = {"StatusCode": 3}
    with pytest.raises(BuildError, match=r"^the build failed \(exit 3\)\. The end of the build log shows why$"):
        builder._compile(strategy, "b", src, None, "codecraft/strategy-bot:abc", out, 1200)
    container.remove.assert_called_once_with(force=True)
    client.api.commit.assert_not_called()
    container.remove.reset_mock()
    container.attrs = {"State": {"OOMKilled": True}}
    container.wait.return_value = {"StatusCode": 137}
    container.logs.return_value = iter([])
    with pytest.raises(BuildError, match=r"ran out of memory \(6144 MB\)"):
        builder._compile(strategy, "b", src, None, "codecraft/strategy-bot:abc", out, 1200)
    container.remove.assert_called_once_with(force=True)


def test_a_compile_past_the_time_limit_is_killed(tmp_path):
    builder, client, container, strategy, src, out, sent = compile_env(tmp_path)
    killed = threading.Event()

    def endless(**kwargs):          # prints one line, then nothing until the container is killed
        yield b"sleep 3000\n"
        killed.wait(10)

    container.logs.side_effect = endless
    container.kill.side_effect = killed.set
    container.wait.return_value = {"StatusCode": 137}
    with pytest.raises(BuildError, match=r"^the build exceeded 1 s and was stopped"):
        builder._compile(strategy, "b", src, None, "codecraft/strategy-bot:abc", out, 1)
    assert killed.is_set()
    container.remove.assert_called_once_with(force=True)
    client.api.commit.assert_not_called()


def test_build_strategy_compiles_once_per_sources_and_base(tmp_path):
    builder, client, container, strategy, src, out, sent = compile_env(tmp_path)
    out.close()
    sdir = strategy.dir
    (sdir / "src").mkdir()
    (sdir / "src" / "my_strategy.py").write_text("mine\n")
    images = {}

    def get(name):
        if name in images:
            return images[name]
        raise docker.errors.ImageNotFound(name)

    client.images.get.side_effect = get
    builder._docker_build = lambda ctx, tag, out, **kw: images.setdefault(tag, MagicMock(id="sha256:base-1"))
    image, build_hash = builder.build_strategy(strategy)
    assert image == f"codecraft/strategy-bot:{build_hash}" and client.containers.create.call_count == 1
    images[image] = MagicMock()
    assert builder.build_strategy(strategy) == (image, build_hash)     # unchanged: no second compile
    assert client.containers.create.call_count == 1
    assert "already built" in builder.log_path(strategy).read_text()
    (sdir / "src" / "my_strategy.py").write_text("changed\n")
    assert builder.build_strategy(strategy)[1] != build_hash and client.containers.create.call_count == 2


def test_the_build_log_keeps_its_end(tmp_path):
    out = _CappedLog(tmp_path / "big.log", 64 * 1024)
    for i in range(20000):
        out.write(f"line {i}\n")
    out.close()
    text = (tmp_path / "big.log").read_text()
    assert len(text) <= 64 * 1024 and text.startswith("[cw] the log is longer than") and text.endswith("line 19999\n")


# Git (security review, finding 9) -----------------------------------------------------------------------------
def test_git_urls_must_be_https_on_a_known_host():
    assert check_git_url(" https://github.com/format37/codecraft-starter ", HOSTS) == \
        "https://github.com/format37/codecraft-starter"
    assert check_git_url("https://GitLab.com/a/b.git", HOSTS)
    for bad in ("http://github.com/a/b", "https://192.168.0.138/a/b", "https://github.com:443/a/b",
                "https://user:pw@github.com/a/b", "https://10.66.0.1/x", "https://localhost/x",
                "https://evil.example/a/b", "https://github.com.evil.example/a/b", "ssh://github.com/a/b", ""):
        with pytest.raises(BuildError, match="^git_url must be an https URL on github.com, gitlab.com, "
                                              "bitbucket.org or codeberg.org; for another host upload a zip$"):
            check_git_url(bad, HOSTS)


def test_git_runs_capped_with_links_off_no_redirects_https_only_and_no_secrets(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["env"], seen["timeout"] = cmd, kwargs["env"], kwargs["timeout"]
        return subprocess.CompletedProcess(cmd, 0, "abc\n", "")

    monkeypatch.setenv("CW_RESEND_API_KEY", "re_secret")
    monkeypatch.setenv("CW_SENTRY_DSN", "https://key@sentry.example/1")
    monkeypatch.setattr(builder_module.subprocess, "run", fake_run)
    assert builder_module._git(tmp_path, "rev-parse", "HEAD", fsize=300 * 2 ** 20) == "abc\n"
    assert seen["cmd"] == ["prlimit", f"--fsize={300 * 2 ** 20}", "git", "-c", "core.symlinks=false",
                           "-c", "http.followRedirects=false", "-c", "protocol.allow=never",
                           "-c", "protocol.https.allow=always", "-C", str(tmp_path), "rev-parse", "HEAD"]
    assert not [k for k in seen["env"] if k.startswith("CW_")] and seen["env"]["GIT_ALLOW_PROTOCOL"] == "https"
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0" and seen["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert seen["timeout"] == 180


def test_git_fetches_one_commit_and_falls_back_for_a_short_commit_id(tmp_path):
    root = make_root(tmp_path)
    builder = Builder(Config(Config.load(root).data, root), client=None)
    calls = []

    def fake_g(repo, *args):
        calls.append(args)
        if args[0] == "fetch" and "--depth" in args and args[-1] in ("abc1234", "nope"):
            raise BuildError(f"git fetch failed with exit code 128:\n\nfatal: couldn't find remote ref {args[-1]}\n")
        if args[:2] == ("rev-parse", "--is-shallow-repository"):
            return "true\n"
        if args[0] == "rev-parse":
            return "abc1234" + "0" * 33 + "\n"
        return ""

    builder._g = fake_g
    builder._git_fetch(tmp_path, None)
    assert calls == [("fetch", "--quiet", "--depth", "1", "--no-tags", "origin", "--end-of-options", "HEAD"),
                     ("checkout", "--quiet", "--force", "FETCH_HEAD"), ("rev-parse", "HEAD")]
    calls.clear()
    builder._git_fetch(tmp_path, "abc1234")
    assert calls[1:] == [("rev-parse", "--is-shallow-repository"), ("fetch", "--quiet", "--no-tags", "--unshallow", "origin"),
                         ("rev-parse", "--verify", "--quiet", "abc1234^{commit}"),
                         ("checkout", "--quiet", "--force", "abc1234" + "0" * 33), ("rev-parse", "HEAD")]
    with pytest.raises(BuildError, match="couldn't find remote ref nope"):      # not a commit id: no fallback
        builder._git_fetch(tmp_path, "nope")


def test_git_failures_name_the_likely_cause_without_server_paths():
    """The live check of 2026-11-06: a missing repository answered with git's "could not read Username"."""
    url = "https://github.com/someone/nope"
    e = git_failure("git fetch --quiet origin HEAD failed with exit code 128:\n\n"
                    "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n", url, None)
    assert str(e).startswith(f"cannot clone {url}: the repository does not exist or is private")
    assert "upload a zip" in str(e) and "--quiet" not in str(e) and "could not read Username" in str(e)
    e = git_failure("git -C <clone> fetch origin deadbeef failed with exit code 128:\n\n"
                    "fatal: couldn't find remote ref deadbeef\n", url, "deadbeef")
    assert str(e).startswith(f"{url} has no commit, branch or tag 'deadbeef'")
    e = git_failure("git x failed with exit code 128:\n\nfatal: unable to access: Could not resolve host\n", url, None)
    assert str(e) == f"git could not fetch {url}\n(git: fatal: unable to access: Could not resolve host)"
    e = git_failure("prlimit --fsize=1 git fetch failed with exit code 128:\n\n"
                    "fatal: fetch-pack: invalid index-pack output\n", url, None, 300)
    assert str(e).startswith("the repository is larger than 300 MB; trim it or upload a zip")
    e = git_failure("git fetch failed with exit code -25:\n\n\n", url, None, 300)
    assert str(e) == "the repository is larger than 300 MB; trim it or upload a zip"


# Symbolic links (security review, finding 1) ----------------------------------------------------------------
def test_a_symbolic_link_in_the_source_is_refused_by_name(tmp_path):
    root = make_root(tmp_path)
    secret = tmp_path / "secret.env"
    secret.write_text("CW_RESEND_API_KEY=re_live\n")
    sdir = seed(root, "s5", "  dir: src\n")
    (sdir / "src" / "lib").mkdir(parents=True)
    (sdir / "src" / "my_strategy.py").write_text("mine\n")
    (sdir / "src" / "lib" / "env.txt").symlink_to(secret)
    builder = Builder(Config(Config.load(root).data, root), client=None)
    with pytest.raises(BuildError, match=r"s5: lib/env\.txt is a symbolic link; links are not accepted"):
        builder._assemble_sources(load_strategy(sdir / "strategy.yaml"), tmp_path / "src5")
    (sdir / "src" / "lib" / "env.txt").unlink()
    (sdir / "src" / "etc").symlink_to(tmp_path, target_is_directory=True)      # a directory link as well
    with pytest.raises(BuildError, match="etc is a symbolic link"):
        builder._assemble_sources(load_strategy(sdir / "strategy.yaml"), tmp_path / "src5b")


def test_a_source_directory_that_is_a_link_out_of_the_tree_is_refused(tmp_path):
    root = make_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "my_strategy.py").write_text("someone else's\n")
    sdir = seed(root, "s6", "  dir: src\n  path: sub\n")
    (sdir / "src").mkdir()
    (sdir / "src" / "sub").symlink_to(outside, target_is_directory=True)
    builder = Builder(Config(Config.load(root).data, root), client=None)
    with pytest.raises(BuildError, match="symbolic link out of the repository"):
        builder._assemble_sources(load_strategy(sdir / "strategy.yaml"), tmp_path / "src6")
    # a language directory that is a link is not taken for the source
    repo = tmp_path / "multi"
    repo.mkdir()
    (repo / "README.md").write_text("doc\n")
    (repo / "python").symlink_to(outside, target_is_directory=True)
    assert language_subdir(repo, "python") == repo


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          check=True, capture_output=True, text=True).stdout


def test_a_link_committed_to_git_is_checked_out_as_a_file_and_refused(tmp_path, monkeypatch):
    """The review's reproduction: a repository with env.txt -> /proc/self/environ put the worker's secrets
    into the build. git now writes the link as a plain file, and the index still names it a link."""
    root = make_root(tmp_path)
    url = "https://github.com/someone/leak"
    repo = tmp_path / "origin"                         # stands for the repository on GitHub
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    (repo / "python").mkdir()
    (repo / "python" / "my_strategy.py").write_text("mine\n")
    (repo / "python" / "env.txt").symlink_to("/proc/self/environ")
    (repo / "README.md").write_text("doc\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "leak")
    sha = git(repo, "rev-parse", "HEAD").strip()

    def local_fetch(self, clone, ref):                 # no network in the tests: fetch from the local "origin"
        subprocess.run(["git", "-C", str(clone), "fetch", "-q", str(repo), "HEAD"], check=True)
        self._g(clone, "checkout", "--quiet", "--force", "FETCH_HEAD")     # as the builder checks out
        self.last_commit = self._g(clone, "rev-parse", "HEAD").strip()

    monkeypatch.setattr(Builder, "_git_fetch", local_fetch)
    removed = []
    monkeypatch.setattr(builder_module.shutil, "rmtree", lambda path, **kw: removed.append(Path(path)))
    sdir = seed(root, "s7", f"  git: {url}\n  ref: {sha}\n")
    builder = Builder(Config(Config.load(root).data, root), client=None)
    clone = root / "data" / "build" / "repos" / "s7"
    with pytest.raises(BuildError, match=r"s7: env\.txt is a symbolic link"):
        builder._assemble_sources(load_strategy(sdir / "strategy.yaml"), tmp_path / "src7")
    written = clone / "python" / "env.txt"
    assert written.is_file() and not written.is_symlink() and written.read_text() == "/proc/self/environ"
    assert builder.last_commit == sha and builder.last_links == {written}
    assert removed == [clone]                          # the checkout does not stay on disk, failed or not
    monkeypatch.undo()
    monkeypatch.setattr(Builder, "_git_fetch", local_fetch)
    # outside the selected folder (another language of a multi-language repository) a link does not matter
    git(repo, "rm", "-q", "python/env.txt")
    (repo / "java").mkdir()
    (repo / "java" / "Link.java").symlink_to("/etc/passwd")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "link elsewhere")
    sha2 = git(repo, "rev-parse", "HEAD").strip()
    (sdir / "strategy.yaml").write_text(f"name: s7\nlanguage: python\nsource:\n  git: {url}\n  ref: {sha2}\n")
    builder._assemble_sources(load_strategy(sdir / "strategy.yaml"), tmp_path / "src7b")
    assert sorted(p.name for p in (tmp_path / "src7b").iterdir()) == ["my_strategy.py"]
    assert not clone.exists()


def test_a_source_is_held_to_the_limits_of_a_zip(tmp_path):
    root = make_root(tmp_path)
    sdir = seed(root, "s8", "  dir: src\n")
    (sdir / "src").mkdir()
    for i in range(3):
        (sdir / "src" / f"f{i}.py").write_bytes(b"x" * 600_000)
    strategy = load_strategy(sdir / "strategy.yaml")
    with pytest.raises(BuildError, match="s8: the source has more than 2 files; trim it"):
        Builder._copy_source(strategy, sdir / "src", tmp_path / "a", max_files=2)
    with pytest.raises(BuildError, match="s8: the source is larger than 1 MB; trim it"):
        Builder._copy_source(strategy, sdir / "src", tmp_path / "b", max_mb=1)
    Builder._copy_source(strategy, sdir / "src", tmp_path / "c", max_files=3, max_mb=2)


def test_the_old_build_timeout_key_still_counts_unless_the_new_one_is_set(tmp_path):
    (tmp_path / "config.yaml").write_text("worker:\n  build_timeout_sec: 77\n")
    cfg = Config.load(tmp_path)
    assert cfg["build"]["timeout_sec"] == 77 and "build_timeout_sec" not in cfg["worker"]
    (tmp_path / "config.yaml").write_text("worker:\n  build_timeout_sec: 77\nbuild:\n  timeout_sec: 900\n")
    assert Config.load(tmp_path)["build"]["timeout_sec"] == 900
    (tmp_path / "config.yaml").write_text("{}\n")
    assert Config.load(tmp_path)["build"]["timeout_sec"] == 1200
