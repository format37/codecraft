from pathlib import Path

import pytest

from codewars.builder import Builder, BuildError, language_subdir
from codewars.config import Config
from codewars.strategies import load_strategy


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
