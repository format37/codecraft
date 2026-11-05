"""Build docker images: the engine and every strategy (official client + strategy source)."""
from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import docker

from .config import Config
from .strategies import Strategy

log = logging.getLogger("codecraft.build")

GIT_URL_RE = re.compile(r"^https?://[\w.\-]+(:\d+)?/[\w./\-~%]+$")
# Terminal colours: docker prints a build step's stderr in red, which is noise in a stored log.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
GIT_NO_ACCESS_RE = re.compile(r"could not read Username|Repository not found|Authentication failed|Access denied"
                              r"|returned error: 40[134]", re.I)
GIT_NO_REF_RE = re.compile(r"pathspec .* did not match|unknown revision|not a commit|reference is not a tree", re.I)


class BuildError(RuntimeError):
    pass


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> str:
    log.debug("$ %s", " ".join(cmd))
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "https:http"}
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise BuildError(f"{' '.join(cmd)} timed out after {timeout}s")
    if proc.returncode != 0:
        raise BuildError(f"{' '.join(cmd)} failed:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def _hash_tree(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(str(path.relative_to(root)).encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:12]


# Files that do not make a root a source tree: a multi-language repository keeps only these at its top.
DOC_FILE_RE = re.compile(r"(?i)^(readme|license|licence|copying|notice|changelog|contributing)(\..*)?$"
                         r"|\.(md|txt|rst)$|^\.(gitignore|gitattributes|editorconfig)$")


def language_subdir(root: Path, language: str) -> Path:
    """A repository with one directory per language (`python/`, `Cpp/`, `java-graalvm-native/`, as the
    starter at github.com/format37/codecraft-starter): when the root holds nothing but documentation,
    the directory named after the chosen language is the source; `python-pypy` falls back to `python/`
    and `cpp-cuda` to `cpp/`. Any other root is used as it is."""
    if any(p.is_file() and not DOC_FILE_RE.match(p.name) for p in root.iterdir()):
        return root
    dirs = {p.name.lower(): p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")}
    for key in (language.lower(), language.lower().split("-")[0]):
        if key in dirs:
            return dirs[key]
    return root


def git_failure(output: str, url: str, ref: str | None) -> BuildError:
    """A failed git command as the submitter reads it: the likely cause first, git's own words after,
    without the command line and the server's clone path."""
    lines = [ln for ln in output.splitlines()[1:] if ln.strip()]       # line 0 is "<command> failed:"
    said = "\n".join(ln for ln in lines if not ln.startswith("hint:"))[:1000]
    if GIT_NO_ACCESS_RE.search(output):
        why = (f"cannot clone {url}: the repository does not exist or is private. The server clones without "
               f"credentials, so make it public or upload a zip instead")
    elif ref and GIT_NO_REF_RE.search(output):
        why = f"{url} has no commit, branch or tag {ref!r}: push it first or give another git_ref"
    else:
        why = f"git could not fetch {url}" + (f" at {ref!r}" if ref else "")
    return BuildError(f"{why}\n(git: {said})" if said else why)


def check_git_url(url: str) -> str:
    url = (url or "").strip()
    if not GIT_URL_RE.match(url):
        raise BuildError(f"only http(s) git URLs are accepted, got {url!r}")
    return url


class Builder:
    def __init__(self, cfg: Config, client: docker.DockerClient):
        self.cfg = cfg
        self.client = client
        self.logs_dir = cfg.build_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.last_commit: str | None = None

    # Engine --------------------------------------------------------------
    def build_engine(self, force: bool = False) -> str:
        image = self.cfg["engine"]["image"]
        if not force and self._image_exists(image):
            return image
        binary = self.cfg.root / self.cfg["engine"]["binary"]
        if not binary.exists():
            raise BuildError(f"engine binary not found: {binary}")
        log.info("building engine image %s", image)
        self._docker_build(binary.parent, image, "engine", nocache=force)
        return image

    # Strategies ----------------------------------------------------------
    def image_name(self, strategy: Strategy, build_hash: str) -> str:
        return f"codecraft/strategy-{strategy.name}:{build_hash}"

    def log_name(self, strategy: Strategy) -> str:
        return f"{strategy.name}-v{strategy.version_number}" if strategy.version_number else strategy.name

    def log_path(self, strategy: Strategy) -> Path:
        return self.logs_dir / f"{self.log_name(strategy)}.log"

    def build_strategy(self, strategy: Strategy, force: bool = False, nocache: bool = False,
                       timeout: int | None = None) -> tuple[str, str]:
        """Returns (image, build_hash). Rebuilds only when the build context changed, unless forced."""
        if strategy.is_builtin:
            raise BuildError(f"{strategy.name} is a built-in bot, nothing to build")
        with tempfile.TemporaryDirectory(prefix=f"cw-build-{strategy.name}-") as tmp:
            ctx = Path(tmp)
            self._assemble_context(strategy, ctx)
            build_hash = _hash_tree(ctx)
            image = self.image_name(strategy, build_hash)
            if not force and self._image_exists(image):
                log.info("%s: image %s is up to date", strategy.name, image)
                self.log_path(strategy).write_text(f"image {image} already built (context unchanged)\n")
                return image, build_hash
            log.info("%s: building %s", strategy.name, image)
            self._docker_build(ctx, image, self.log_name(strategy), nocache=nocache,
                               timeout=timeout or int(self.cfg.get("worker", "build_timeout_sec") or 1200))
            return image, build_hash

    def _assemble_context(self, strategy: Strategy, ctx: Path) -> None:
        base_dockerfile, client_dir = self._resolve_language(strategy.language)
        shutil.copytree(client_dir, ctx / "client", ignore=shutil.ignore_patterns(".git", "__pycache__"))
        src_root = self._fetch_source(strategy)
        self._copy_source(strategy, src_root, ctx / "src")
        if strategy.overlay:
            overlay = strategy.dir / strategy.overlay
            if not overlay.is_dir():
                raise BuildError(f"{strategy.name}: overlay directory {overlay} not found")
            shutil.copytree(overlay, ctx / "src", dirs_exist_ok=True)
        lines = base_dockerfile.read_text().splitlines()
        lines = [("COPY client /project" if l.strip() == "COPY . /project" else l) for l in lines]
        merge = strategy.source.merge
        src_dir = "/project" if merge else "/src"
        lines += ["", "# --- added by codecraft ---", "RUN mkdir -p /output /src"]
        # merge mode: the strategy's files land on top of the official client and compile.sh is run
        # with "base", which skips its own copy step (that step replaces the whole client otherwise)
        lines.append("COPY src /project/" if merge else "COPY src /src")
        if strategy.prebuild:
            prebuild = strategy.dir / strategy.prebuild
            if not prebuild.is_file():
                raise BuildError(f"{strategy.name}: prebuild script {prebuild} not found")
            shutil.copy(prebuild, ctx / "cw-prebuild.sh")
            lines += ["COPY cw-prebuild.sh /cw-prebuild.sh", f"RUN cd {src_dir} && bash /cw-prebuild.sh"]
        lines += [
            # compile.sh wipes /project when the source has several files, so keep run.sh outside it
            f"RUN cp /project/run.sh /cw-run.sh && cd /project && bash compile.sh{' base' if merge else ''}",
            'ENTRYPOINT ["bash", "/cw-run.sh"]',
        ]
        (ctx / "Dockerfile").write_text("\n".join(lines) + "\n")

    def _resolve_language(self, language: str) -> tuple[Path, Path]:
        """-> (base Dockerfile, official client directory)."""
        template = self.cfg.templates_dir / language
        clients = {p.name.lower(): p for p in self.cfg.clients_dir.iterdir() if p.is_dir()}
        if (template / "Dockerfile").is_file():
            client_name = language
            marker = template / "client"
            if marker.is_file():
                client_name = marker.read_text().strip()
            client_dir = clients.get(client_name.lower())
            if client_dir is None:
                raise BuildError(f"template {language}: client {client_name!r} not found in {self.cfg.clients_dir}")
            return template / "Dockerfile", client_dir
        client_dir = clients.get(language.lower())
        if client_dir is None or not (client_dir / "Dockerfile").is_file():
            raise BuildError(f"unknown language {language!r}: no templates/{language}/Dockerfile and no clients/{language}")
        return client_dir / "Dockerfile", client_dir

    def _git_checkout(self, repo: Path, url: str, ref: str | None) -> None:
        try:
            _run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref or 'HEAD'}^{{commit}}"])
        except BuildError:
            _run(["git", "-C", str(repo), "fetch", "--quiet", "origin"])
        if ref:
            _run(["git", "-C", str(repo), "checkout", "--quiet", "--force", ref])
        else:
            _run(["git", "-C", str(repo), "pull", "--quiet", "--ff-only"])
        self.last_commit = _run(["git", "-C", str(repo), "rev-parse", "HEAD"]).strip()

    def _fetch_source(self, strategy: Strategy) -> Path:
        src = strategy.source
        self.last_commit = None
        if src.dir:
            root = strategy.source_root()
            if not root.is_dir():
                raise BuildError(f"{strategy.name}: source dir {root} not found")
        else:
            url = check_git_url(src.git)
            repo = self.cfg.build_dir / "repos" / strategy.name
            if (repo / ".git").is_dir():
                origin = _run(["git", "-C", str(repo), "remote", "get-url", "origin"]).strip()
                if origin != url:
                    shutil.rmtree(repo)
            if not (repo / ".git").is_dir():
                repo.parent.mkdir(parents=True, exist_ok=True)
                log.info("%s: cloning %s", strategy.name, url)
                try:
                    _run(["git", "clone", "--quiet", url, str(repo)])
                except BuildError as exc:
                    raise git_failure(str(exc).replace(str(repo), "<clone>"), url, src.ref) from None
            try:
                self._git_checkout(repo, url, src.ref)
            except BuildError as exc:
                raise git_failure(str(exc).replace(str(repo), "<clone>"), url, src.ref) from None
            root = repo
        if src.path:
            root = root / src.path
            if not root.is_dir():
                raise BuildError(f"{strategy.name}: source.path {root} not found")
        if strategy.language:
            root = language_subdir(root, strategy.language)
        return root

    @staticmethod
    def _copy_source(strategy: Strategy, root: Path, dst: Path) -> None:
        include = strategy.source.include
        dst.mkdir(parents=True)
        for path in root.rglob("*"):
            rel = path.relative_to(root)
            if ".git" in rel.parts or "__pycache__" in rel.parts:
                continue
            if include and not any(fnmatch.fnmatch(str(rel), pat) for pat in include):
                continue
            target = dst / rel
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        if not any(dst.iterdir()):
            raise BuildError(f"{strategy.name}: no source files selected from {root} (include={include})")

    # Docker --------------------------------------------------------------
    def _image_exists(self, image: str) -> bool:
        try:
            self.client.images.get(image)
            return True
        except docker.errors.ImageNotFound:
            return False

    def _docker_build(self, ctx: Path, tag: str, log_name: str, nocache: bool = False, timeout: int = 1200) -> None:
        log_file = self.logs_dir / f"{log_name}.log"
        error = None
        with log_file.open("w") as fh:
            try:
                for chunk in self.client.api.build(path=str(ctx), tag=tag, rm=True, nocache=nocache, decode=True,
                                                   timeout=timeout):
                    if "stream" in chunk:
                        stream = ANSI_RE.sub("", chunk["stream"])
                        fh.write(stream)
                        text = stream.rstrip()
                        if text:
                            log.info("  %s", text[:200])
                    elif "error" in chunk:
                        error = ANSI_RE.sub("", chunk.get("errorDetail", {}).get("message") or chunk["error"])
                        fh.write(f"ERROR: {error}\n")
            except Exception as exc:  # noqa: BLE001 - e.g. a read timeout on a silent build step
                error = f"{type(exc).__name__}: {exc}"
                fh.write(f"ERROR: {error}\n")
        if error:
            raise BuildError(f"the build failed: {error}\nThe end of the build log shows why.")
