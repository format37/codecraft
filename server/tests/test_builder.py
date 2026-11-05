import subprocess
from pathlib import Path

import pytest

from codecraft import builder as builder_module
from codecraft.builder import Builder, BuildError, git_failure, language_subdir
from codecraft.config import Config
from codecraft.strategies import load_strategy


def make_root(tmp_path: Path) -> Path:
    (tmp_path / "clients" / "Python").mkdir(parents=True)
    (tmp_path / "clients" / "Python" / "Dockerfile").write_text("FROM python:3.8\nCOPY . /project\nWORKDIR /project\n")
    (tmp_path / "clients" / "Python" / "compile.sh").write_text("cp -r * /output/\n")
    (tmp_path / "clients" / "Python" / "run.sh").write_text("python main.py\n")
    (tmp_path / "clients" / "Python" / "my_strategy.py").write_text("stock\n")
    (tmp_path / "templates").mkdir()
    (tmp_path / "data").mkdir()
    return tmp_path


def test_context_from_local_dir_with_include_and_overlay(tmp_path):
    root = make_root(tmp_path)
    sdir = root / "strategies" / "s1"
    (sdir / "src").mkdir(parents=True)
    (sdir / "src" / "my_strategy.py").write_text("mine\n")
    (sdir / "src" / "README.md").write_text("ignored\n")
    (sdir / "overlay").mkdir()
    (sdir / "overlay" / "extra.txt").write_text("overlay\n")
    (sdir / "pre.sh").write_text("echo hi\n")
    (sdir / "strategy.yaml").write_text(
        "name: s1\nlanguage: python\nsource:\n  dir: src\n  include: [my_strategy.py]\noverlay: overlay\nprebuild: pre.sh\n")
    cfg = Config(Config.load(root).data, root)
    builder = Builder(cfg, client=None)
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    builder._assemble_context(load_strategy(sdir / "strategy.yaml"), ctx)
    assert (ctx / "client" / "my_strategy.py").read_text() == "stock\n"
    assert (ctx / "src" / "my_strategy.py").read_text() == "mine\n"
    assert not (ctx / "src" / "README.md").exists()
    assert (ctx / "src" / "extra.txt").read_text() == "overlay\n"
    dockerfile = (ctx / "Dockerfile").read_text()
    assert "COPY client /project" in dockerfile and "COPY . /project" not in dockerfile
    assert "cw-prebuild.sh" in dockerfile and dockerfile.rstrip().endswith('ENTRYPOINT ["bash", "/cw-run.sh"]')
    assert "cp /project/run.sh /cw-run.sh" in dockerfile


def test_unknown_language_is_an_error(tmp_path):
    root = make_root(tmp_path)
    sdir = root / "strategies" / "s2"
    (sdir / "src").mkdir(parents=True)
    (sdir / "src" / "x.rs").write_text("")
    (sdir / "strategy.yaml").write_text("name: s2\nlanguage: brainfuck\nsource:\n  dir: src\n")
    builder = Builder(Config(Config.load(root).data, root), client=None)
    with pytest.raises(BuildError):
        builder._assemble_context(load_strategy(sdir / "strategy.yaml"), tmp_path / "ctx2")


def test_merge_mode_overlays_the_client_and_skips_compile_copy(tmp_path):
    root = make_root(tmp_path)
    sdir = root / "strategies" / "s3"
    (sdir / "src").mkdir(parents=True)
    (sdir / "src" / "kernel.cu").write_text("__global__ void k() {}\n")
    (sdir / "strategy.yaml").write_text(
        "name: s3\nlanguage: python\nlimits: {memory_mb: 2048}\nsource:\n  dir: src\n  merge: true\n")
    strategy = load_strategy(sdir / "strategy.yaml")
    assert strategy.source.merge and strategy.limits == {"memory_mb": 2048}
    builder = Builder(Config(Config.load(root).data, root), client=None)
    ctx = tmp_path / "ctx3"
    ctx.mkdir()
    builder._assemble_context(strategy, ctx)
    dockerfile = (ctx / "Dockerfile").read_text()
    assert "COPY src /project/" in dockerfile and "COPY src /src" not in dockerfile
    assert "bash compile.sh base" in dockerfile


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


def test_git_failures_name_the_likely_cause_without_server_paths():
    """The live check of 2026-11-06: a missing repository answered with git's "could not read Username"."""
    url = "https://github.com/someone/nope"
    e = git_failure("git clone --quiet https://github.com/someone/nope <clone> failed:\n\n"
                    "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n", url, None)
    assert str(e).startswith(f"cannot clone {url}: the repository does not exist or is private")
    assert "upload a zip" in str(e) and "--quiet" not in str(e) and "could not read Username" in str(e)
    e = git_failure("git -C <clone> checkout --quiet --force deadbeef failed:\n\n"
                    "error: pathspec 'deadbeef' did not match any file(s) known to git\n", url, "deadbeef")
    assert str(e).startswith(f"{url} has no commit, branch or tag 'deadbeef'")
    e = git_failure("git clone x failed:\n\nfatal: unable to access: Could not resolve host\n", url, None)
    assert str(e) == f"git could not fetch {url}\n(git: fatal: unable to access: Could not resolve host)"


# Symbolic links (security review, finding 1) ----------------------------------------------------------------
def seed(root: Path, name: str, source: str) -> Path:
    sdir = root / "strategies" / name
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "strategy.yaml").write_text(f"name: {name}\nlanguage: python\nsource:\n{source}")
    return sdir


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
        builder._assemble_context(load_strategy(sdir / "strategy.yaml"), tmp_path / "ctx5")
    (sdir / "src" / "lib" / "env.txt").unlink()
    (sdir / "src" / "etc").symlink_to(tmp_path, target_is_directory=True)      # a directory link as well
    with pytest.raises(BuildError, match="etc is a symbolic link"):
        builder._assemble_context(load_strategy(sdir / "strategy.yaml"), tmp_path / "ctx5b")


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
        builder._assemble_context(load_strategy(sdir / "strategy.yaml"), tmp_path / "ctx6")
    # a language directory that is a link is not taken for the source
    repo = tmp_path / "multi"
    repo.mkdir()
    (repo / "README.md").write_text("doc\n")
    (repo / "python").symlink_to(outside, target_is_directory=True)
    assert language_subdir(repo, "python") == repo


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          check=True, capture_output=True, text=True).stdout


def test_a_link_committed_to_git_is_checked_out_as_a_file_and_refused(tmp_path):
    """The review's reproduction: a repository with env.txt -> /proc/self/environ put the worker's secrets
    into the build. git now writes the link as a plain file, and the index still names it a link."""
    root = make_root(tmp_path)
    url = "https://github.com/someone/leak"
    repo = root / "data" / "build" / "repos" / "s7"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "remote", "add", "origin", url)
    (repo / "python").mkdir()
    (repo / "python" / "my_strategy.py").write_text("mine\n")
    (repo / "python" / "env.txt").symlink_to("/proc/self/environ")
    (repo / "README.md").write_text("doc\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "leak")
    sha = git(repo, "rev-parse", "HEAD").strip()
    (repo / "python" / "env.txt").unlink()            # the checkout below writes it again, as git is told to
    sdir = seed(root, "s7", f"  git: {url}\n  ref: {sha}\n")
    builder = Builder(Config(Config.load(root).data, root), client=None)
    with pytest.raises(BuildError, match=r"s7: env\.txt is a symbolic link"):
        builder._assemble_context(load_strategy(sdir / "strategy.yaml"), tmp_path / "ctx7")
    written = repo / "python" / "env.txt"
    assert written.is_file() and not written.is_symlink() and written.read_text() == "/proc/self/environ"
    assert builder.last_commit == sha and builder.last_links == {written}
    # outside the selected folder (another language of a multi-language repository) a link does not matter
    git(repo, "rm", "-qf", "python/env.txt")    # -f: on disk it is a plain file now, the index says link
    (repo / "java").mkdir()
    (repo / "java" / "Link.java").symlink_to("/etc/passwd")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "link elsewhere")
    sha2 = git(repo, "rev-parse", "HEAD").strip()
    (sdir / "strategy.yaml").write_text(f"name: s7\nlanguage: python\nsource:\n  git: {url}\n  ref: {sha2}\n")
    ctx = tmp_path / "ctx7b"
    ctx.mkdir()
    builder._assemble_context(load_strategy(sdir / "strategy.yaml"), ctx)
    assert sorted(p.name for p in (ctx / "src").iterdir()) == ["my_strategy.py"]


def test_git_runs_with_links_off_and_without_the_server_secrets(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["env"] = cmd, kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, "abc\n", "")

    monkeypatch.setenv("CW_RESEND_API_KEY", "re_secret")
    monkeypatch.setenv("CW_SENTRY_DSN", "https://key@sentry.example/1")
    monkeypatch.setattr(builder_module.subprocess, "run", fake_run)
    assert builder_module._git(tmp_path, "rev-parse", "HEAD") == "abc\n"
    assert seen["cmd"][:5] == ["git", "-c", "core.symlinks=false", "-C", str(tmp_path)]
    assert not [k for k in seen["env"] if k.startswith("CW_")] and seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["env"]["GIT_CONFIG_NOSYSTEM"] == "1" and seen["env"]["PATH"]
