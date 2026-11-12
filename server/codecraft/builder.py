"""Build docker images: the engine, a base image per language, and every strategy.

A strategy is built in two steps. The base image of its language is the template's or the official client's
Dockerfile with the client copied in: trusted files only, an ordinary docker build with the network and the
layer cache, once per language (`cw build` warms them all). The strategy's own compile.sh then runs in a
container made from that base with no network, memory, CPU and process limits, no capabilities and a hard wall
clock; that container is committed as the strategy's image, so a game runs exactly what compiled there. The
2020 testing system compiled offline too: everything a client needs is fetched by its base image."""
from __future__ import annotations

import codecs
import fnmatch
import hashlib
import io
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path

import docker
from docker.types import LogConfig

from .config import Config
from .strategies import Strategy

log = logging.getLogger("codecraft.build")

# https only, a bare host name (no port, no user:password@), then a path
GIT_URL_RE = re.compile(r"^https://(?P<host>[A-Za-z0-9][A-Za-z0-9.\-]*)/[\w./\-~%]+$")
# Terminal colours: docker prints a build step's stderr in red, which is noise in a stored log.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
GIT_NO_ACCESS_RE = re.compile(r"could not read Username|Repository not found|Authentication failed|Access denied"
                              r"|returned error: 40[134]", re.I)
GIT_NO_REF_RE = re.compile(r"pathspec .* did not match|unknown revision|not a commit|reference is not a tree"
                           r"|couldn't find remote ref|not our ref|Needed a single revision", re.I)
# prlimit --fsize stops the pack download: git reports its index-pack child dying
GIT_TOO_BIG_RE = re.compile(r"invalid index-pack output|index-pack died|File size limit exceeded|signal 25\b"
                            r"|exit code (-25|153)\b", re.I)
GIT_MOVED_RE = re.compile(r"returned error: 30[1278]")
SHORT_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")

# Part of every strategy image's hash: change it when the compile step changes, so every strategy rebuilds.
BUILD_RECIPE = "compile-container-1"
# The compile container has no network: Maven must not try it (java, kotlin, scala and the GraalVM variants).
MAVEN_OFFLINE = ("RUN mkdir -p /root/.m2 && printf '<settings><offline>true</offline></settings>' "
                 "> /root/.m2/settings.xml")
IGNORED = shutil.ignore_patterns(".git", "__pycache__")


class BuildError(RuntimeError):
    pass


# Git -----------------------------------------------------------------------------------------------------------
# Every git command: a symbolic link in a repository is checked out as a plain file holding the link's target
# (_copy_source refuses links as well), no redirect is followed, and https is the only transport.
GIT_OPTIONS = ("-c", "core.symlinks=false", "-c", "http.followRedirects=false",
               "-c", "protocol.allow=never", "-c", "protocol.https.allow=always")
GIT_TIMEOUT_SEC = 180


def _git_env() -> dict[str, str]:
    """git's whole environment: none of the server's CW_* secrets, no system or user configuration."""
    return {"PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "LANG": "C",
            "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ALLOW_PROTOCOL": "https"}


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 600, env: dict[str, str] | None = None) -> str:
    log.debug("$ %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise BuildError(f"{' '.join(cmd)} timed out after {timeout}s")
    if proc.returncode != 0:
        raise BuildError(f"{' '.join(cmd)} failed with exit code {proc.returncode}:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def _git(repo: Path | None, *args: str, fsize: int | None = None, timeout: int = GIT_TIMEOUT_SEC) -> str:
    """Run git (in `repo` unless None) with GIT_OPTIONS and the minimal environment. `fsize` caps every file
    git and its children write (RLIMIT_FSIZE through prlimit: the pack of a fetch is the big one)."""
    cmd = ["git", *GIT_OPTIONS] + (["-C", str(repo)] if repo is not None else []) + list(args)
    if fsize:
        cmd = ["prlimit", f"--fsize={int(fsize)}", *cmd]
    return _run(cmd, timeout=timeout, env=_git_env())


def _git_links(repo: Path) -> set[Path]:
    """The paths the checked-out commit records as symbolic links (mode 120000). With core.symlinks=false
    they are plain files on disk, so only the index still tells them apart."""
    links = set()
    for entry in _git(repo, "ls-files", "--stage", "-z").split("\0"):
        meta, _, path = entry.partition("\t")
        if path and meta.split(" ", 1)[0] == "120000":
            links.add(repo / path)
    return links


def git_failure(output: str, url: str, ref: str | None, max_mb: int | None = None) -> BuildError:
    """A failed git command as the submitter reads it: the likely cause first, git's own words after,
    without the command line and the server's clone path."""
    first, _, rest = output.partition("\n")                 # line 0 is "<command> failed with exit code N:"
    lines = [ln for ln in rest.splitlines() if ln.strip()]
    said = "\n".join(ln for ln in lines if not ln.startswith("hint:"))[:1000]
    if GIT_NO_ACCESS_RE.search(output):
        why = (f"cannot clone {url}: the repository does not exist or is private. The server clones without "
               f"credentials, so make it public or upload a zip instead")
    elif GIT_TOO_BIG_RE.search(output) or GIT_TOO_BIG_RE.search(first):
        why = f"the repository is larger than {max_mb or '?'} MB; trim it or upload a zip"
    elif GIT_MOVED_RE.search(output):
        why = f"{url} answers with a redirect (the repository moved?): give its current URL"
    elif ref and GIT_NO_REF_RE.search(output):
        why = f"{url} has no commit, branch or tag {ref!r}: push it first or give another git_ref"
    else:
        why = f"git could not fetch {url}" + (f" at {ref!r}" if ref else "")
    return BuildError(f"{why}\n(git: {said})" if said else why)


def check_git_url(url: str, hosts: list[str] | tuple[str, ...]) -> str:
    """A public repository on one of `hosts` (submissions.git_hosts): https, no port, no credentials."""
    url = (url or "").strip()
    m = GIT_URL_RE.match(url)
    if not m or m["host"].lower() not in {h.lower() for h in hosts}:
        names = list(hosts)
        listed = f"{', '.join(names[:-1])} or {names[-1]}" if len(names) > 1 else "".join(names)
        raise BuildError(f"git_url must be an https URL on {listed}; for another host upload a zip")
    return url


# Files ---------------------------------------------------------------------------------------------------------
def _hash_tree(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root)
        if ".git" in rel.parts or "__pycache__" in rel.parts:
            continue
        h.update(str(rel).encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:12]


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:12]


# The engine image ------------------------------------------------------------------------------------------------
COPY_FLAGS_RE = re.compile(r"((?:--\S+\s+)*)(.*)", re.S)


def dockerfile_sources(text: str) -> list[str]:
    """The context paths that a Dockerfile's COPY and ADD instructions take, as written (files, directories,
    globs): not what they take from another stage or image (`--from`), nor URLs."""
    sources = []
    for line in re.sub(r"\\[ \t]*\r?\n", " ", text).splitlines():
        words = line.strip().split(None, 1)
        if len(words) < 2 or words[0].upper() not in ("COPY", "ADD"):
            continue
        flags, body = COPY_FLAGS_RE.match(words[1].strip()).groups()
        if any(flag.startswith("--from") for flag in flags.split()):
            continue
        args = json.loads(body) if body.startswith("[") else body.split()
        sources += [a for a in args[:-1] if "://" not in a and not a.startswith("<<")]
    return sources


def engine_source(context: Path) -> dict[str, bytes]:
    """What an engine image is built from: its Dockerfile and every context file that its COPY and ADD instructions
    take (directories in full, bytecode caches aside), by their '/'-separated paths in the context."""
    root = context.resolve()
    if not (root / "Dockerfile").is_file():
        raise BuildError(f"no Dockerfile in the engine context {context}")
    files = {"Dockerfile": (root / "Dockerfile").read_bytes()}
    for src in dockerfile_sources(files["Dockerfile"].decode("utf-8", "replace")):
        rel = src.lstrip("/")
        paths = sorted(root.glob(rel)) if any(c in rel for c in "*?[") else [root / rel]
        if not paths or not all(p.exists() and p.resolve().is_relative_to(root) for p in paths):
            raise BuildError(f"the engine's Dockerfile copies {src}, which is not in {context}")
        for path in paths:
            for p in (path.rglob("*") if path.is_dir() else [path]):
                if p.is_file() and not p.is_symlink() and "__pycache__" not in p.relative_to(root).parts:
                    files[p.relative_to(root).as_posix()] = p.read_bytes()
    return files


def source_version(files: dict[str, bytes]) -> str:
    """The version of an engine's source: 12 hex digits of the sha256 of its files' `sha256sum` lines in the order
    of their paths. sim-cpp/Makefile computes the same for the version the engine reports (`--version`)."""
    lines = "".join(f"{hashlib.sha256(data).hexdigest()}  {path}\n" for path, data in sorted(files.items()))
    return hashlib.sha256(lines.encode()).hexdigest()[:12]


def pinned(image: str) -> bool:
    """An image named with a tag or a digest of its own (`codecraft/sim:0.1.0`, not `localhost:5000/engine`)."""
    return "@" in image or ":" in image.rsplit("/", 1)[-1]


def engine_context(cfg: Config) -> Path:
    """The directory the engine image is built from: `engine.context`, a directory with a Dockerfile (`sim-cpp`, our
    own engine, by default)."""
    context = cfg["engine"].get("context")
    if not context:
        raise BuildError("engine.context is not set: the directory the engine image is built from")
    return cfg.root / context


def engine_image(cfg: Config, files: dict[str, bytes] | None = None) -> str:
    """The engine image of the current source: `engine.image` tagged with the version of its source (`files`, read
    when not given), or as it is when it names a tag of its own (pinned: built when missing, whatever the source)."""
    image = cfg["engine"]["image"]
    if pinned(image):
        return image
    return f"{image}:{source_version(engine_source(engine_context(cfg)) if files is None else files)}"


class EngineImage:
    """The engine image that games are played on, kept to its source: before each game, a source that changed (a
    deploy, a git pull) is built first. While a changed source does not build, games go on with the image they
    had and that source is not tried again until it changes; the error goes to the log, and so to Sentry."""

    def __init__(self, builder: Builder):
        self.builder = builder
        self.lock = threading.Lock()
        self.image: str | None = None       # what games run on
        self.failed: str | None = None      # the image of a source that did not build

    def current(self) -> str:
        with self.lock:
            wanted = None
            try:
                wanted = engine_image(self.builder.cfg)
                if wanted not in (self.image, self.failed):
                    self.image, self.failed = self.builder.build_engine(), None
            except (OSError, BuildError):
                if self.image is None:
                    raise
                if wanted is None or wanted != self.failed:
                    log.exception("the engine %s cannot be built: games go on with %s",
                                  wanted or "(its source cannot be read)", self.image)
                self.failed = wanted
            return self.image


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
    dirs = {p.name.lower(): p for p in root.iterdir()
            if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")}
    for key in (language.lower(), language.lower().split("-")[0]):
        if key in dirs:
            return dirs[key]
    return root


def compile_command(merge: bool, prebuild: bool) -> str:
    """What the compile container runs: a seed's prebuild script, then the client's compile.sh, with "base" in
    merge mode (the files are already on top of the client, and compile.sh skips its copy of /src)."""
    src_dir = "/project" if merge else "/src"
    steps = [f"cd {src_dir} && bash /cw-prebuild.sh"] if prebuild else []
    # compile.sh wipes /project when the source has several files, so keep run.sh outside it
    steps += ["cp /project/run.sh /cw-run.sh", f"cd /project && bash compile.sh{' base' if merge else ''}"]
    return " && ".join(steps)


def _tar_sources(src: Path, prefix: str, prebuild: str | None, fileobj) -> None:
    """The assembled sources as `<prefix>/...` (owned by root, plain modes) plus /cw-prebuild.sh, for
    put_archive("/"). Only directories and regular files: _copy_source has refused links."""
    now = int(time.time())
    with tarfile.open(fileobj=fileobj, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(src.rglob("*")):
            st = path.lstat()
            info = tarfile.TarInfo(f"{prefix}/{path.relative_to(src).as_posix()}")
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = int(st.st_mtime)
            if stat.S_ISDIR(st.st_mode):
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            elif stat.S_ISREG(st.st_mode):
                info.size, info.mode = st.st_size, 0o755 if st.st_mode & 0o111 else 0o644
                with path.open("rb") as fh:
                    tar.addfile(info, fh)
            else:
                raise BuildError(f"{path.relative_to(src)} is not a regular file")
        if prebuild is not None:
            data = prebuild.encode()
            info = tarfile.TarInfo("cw-prebuild.sh")
            info.size, info.mode, info.mtime = len(data), 0o755, now
            tar.addfile(info, io.BytesIO(data))


class _CappedLog:
    """A build log on disk that keeps at most about `cap` bytes: past it the beginning is dropped (the end
    of a log tells why a build failed). Line-buffered, so version(log_tail) follows a running build."""

    def __init__(self, path: Path, cap: int):
        self.path, self.cap = path, max(int(cap), 64 * 1024)
        self.fh = path.open("w", encoding="utf-8", errors="replace", buffering=1)
        self.size = 0

    def write(self, text: str) -> None:
        self.fh.write(text)
        self.size += len(text)
        if self.size > self.cap:
            self._trim()

    def _trim(self) -> None:
        self.fh.close()
        keep = self.path.read_bytes()[-(self.cap // 2):]
        cut = keep.find(b"\n")
        if 0 <= cut < 4096:
            keep = keep[cut + 1:]
        note = f"[cw] the log is longer than {self.cap // 2 ** 20} MB: its beginning was dropped\n".encode()
        self.path.write_bytes(note + keep)
        self.fh = self.path.open("a", encoding="utf-8", errors="replace", buffering=1)
        self.size = len(note) + len(keep)

    def close(self) -> None:
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _follow(container, out: _CappedLog) -> None:
    """Copy the container's output into the log as it comes, without terminal colours."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    for chunk in container.logs(stream=True, follow=True):
        pending += decoder.decode(chunk)
        *lines, pending = pending.split("\n")
        if len(pending) > 65536:                    # a "line" without end must not grow in memory
            lines.append(pending)
            pending = ""
        for line in lines:
            out.write(ANSI_RE.sub("", line) + "\n")
    pending += decoder.decode(b"", final=True)
    if pending:
        out.write(ANSI_RE.sub("", pending) + "\n")


class Builder:
    def __init__(self, cfg: Config, client: docker.DockerClient):
        self.cfg = cfg
        self.client = client
        self.logs_dir = cfg.build_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.last_commit: str | None = None
        self.last_links: set[Path] = set()      # links recorded in the last git checkout (_git_links)
        sub = cfg["submissions"]
        self.git_hosts = list(sub.get("git_hosts") or [])
        self.git_max_mb = int(float(sub.get("max_unpacked_mb") or 200) * 1.5)

    @property
    def log_cap(self) -> int:
        return int(float(self.cfg.get("build", "log_max_mb", default=16) or 16) * 2 ** 20)

    # Engine --------------------------------------------------------------
    def build_engine(self, force: bool = False) -> str:
        """The engine image of the current source (engine_image), built when missing (or always with `force`) from
        exactly the files that its tag is the hash of: a copy of them, so a change while it builds is not mixed in."""
        ctx = engine_context(self.cfg)
        files = None if pinned(self.cfg["engine"]["image"]) else engine_source(ctx)
        image = engine_image(self.cfg, files)
        if not force and self._image_exists(image):
            return image
        files = engine_source(ctx) if files is None else files
        log.info("building engine image %s from %s", image, ctx)
        with tempfile.TemporaryDirectory(prefix="cw-engine-") as tmp, \
                _CappedLog(self.logs_dir / "engine.log", self.log_cap) as out:
            for rel, data in files.items():
                path = Path(tmp) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                if os.access(ctx / rel, os.X_OK):
                    path.chmod(0o755)
            self._docker_build(Path(tmp), image, out, nocache=force)
        return image

    # Base images (trusted: template or client Dockerfile + the official client) ---------------------------
    def base_dockerfile(self, language: str) -> tuple[str, Path]:
        """The base image's Dockerfile and the official client directory it copies."""
        dockerfile, client_dir = self._resolve_language(language)
        lines = [("COPY client /project" if line.strip() == "COPY . /project" else line)
                 for line in dockerfile.read_text().splitlines()]
        lines += ["", "# --- added by codecraft: strategies compile in a container made from this image ---",
                  "RUN mkdir -p /output /src"]
        if (client_dir / "pom.xml").is_file():
            lines.append(MAVEN_OFFLINE)
        return "\n".join(lines) + "\n", client_dir

    def base_image(self, language: str) -> str:
        text, client_dir = self.base_dockerfile(language)
        return f"codecraft/base-{language.lower()}:{_digest(text, client_dir.name, _hash_tree(client_dir))}"

    def build_base(self, language: str, out: _CappedLog | None = None, nocache: bool = False) -> str:
        """The base image of a language, built when missing (or always with `nocache`)."""
        image = self.base_image(language)
        if not nocache and self._image_exists(image):
            return image
        text, client_dir = self.base_dockerfile(language)
        own = out is None
        if own:
            out = _CappedLog(self.logs_dir / f"base-{language.lower()}.log", self.log_cap)
        try:
            log.info("building base image %s", image)
            out.write(f"[cw] building the {language} toolchain image {image}: once per language, from the "
                      f"template and the official client\n")
            with tempfile.TemporaryDirectory(prefix=f"cw-base-{language}-") as tmp:
                ctx = Path(tmp)
                shutil.copytree(client_dir, ctx / "client", ignore=IGNORED)
                (ctx / "Dockerfile").write_text(text)
                self._docker_build(ctx, image, out, nocache=nocache, timeout=3600)
        finally:
            if own:
                out.close()
        return image

    # Strategies ----------------------------------------------------------
    def image_name(self, strategy: Strategy, build_hash: str) -> str:
        return f"codecraft/strategy-{strategy.name}:{build_hash}"

    def log_name(self, strategy: Strategy) -> str:
        return f"{strategy.name}-v{strategy.version_number}" if strategy.version_number else strategy.name

    def log_path(self, strategy: Strategy) -> Path:
        return self.logs_dir / f"{self.log_name(strategy)}.log"

    def build_strategy(self, strategy: Strategy, force: bool = False, nocache: bool = False,
                       timeout: int | None = None, log_file: Path | None = None) -> tuple[str, str]:
        """Returns (image, build_hash). Compiles only when the sources, the base image or the recipe changed,
        unless forced; `nocache` rebuilds the base image too."""
        if strategy.is_builtin:
            raise BuildError(f"{strategy.name} is a built-in bot, nothing to build")
        timeout = int(timeout or self.cfg.get("build", "timeout_sec", default=1200) or 1200)
        with _CappedLog(log_file or self.log_path(strategy), self.log_cap) as out, \
                tempfile.TemporaryDirectory(prefix=f"cw-build-{strategy.name}-") as tmp:
            src = Path(tmp) / "src"
            out.write(f"[cw] {self.log_name(strategy)}: {strategy.language}"
                      f"{', merge mode' if strategy.source.merge else ''}\n")
            self._assemble_sources(strategy, src)
            if self.last_commit:
                out.write(f"[cw] source: {strategy.source.git} at {self.last_commit}\n")
            prebuild = self._prebuild_text(strategy)
            base = self.build_base(strategy.language, out, nocache=nocache)
            base_id = self.client.images.get(base).id
            merge = "merge" if strategy.source.merge else "plain"
            build_hash = _digest(BUILD_RECIPE, base_id, _hash_tree(src), merge, prebuild or "")
            image = self.image_name(strategy, build_hash)
            if not force and self._image_exists(image):
                log.info("%s: image %s is up to date", strategy.name, image)
                out.write(f"image {image} already built (sources and toolchain unchanged)\n")
                return image, build_hash
            log.info("%s: compiling %s", strategy.name, image)
            self._compile(strategy, base, src, prebuild, image, out, timeout)
            return image, build_hash

    def _assemble_sources(self, strategy: Strategy, dst: Path) -> None:
        """The strategy's files as compile.sh sees them in /src (or on top of /project in merge mode)."""
        self._resolve_language(strategy.language)        # an unknown language fails before any git work
        sub = self.cfg["submissions"]
        try:
            src_root = self._fetch_source(strategy)
            self._copy_source(strategy, src_root, dst, self.last_links, max_files=int(sub.get("max_files") or 2000),
                              max_mb=int(sub.get("max_unpacked_mb") or 200))
        finally:
            if strategy.source.git:     # one commit deep, cheap to fetch again: no checkout stays on disk
                shutil.rmtree(self.cfg.build_dir / "repos" / strategy.name, ignore_errors=True)
        if strategy.overlay:
            overlay = strategy.dir / strategy.overlay
            if not overlay.is_dir():
                raise BuildError(f"{strategy.name}: overlay directory {overlay} not found")
            shutil.copytree(overlay, dst, dirs_exist_ok=True)

    @staticmethod
    def _prebuild_text(strategy: Strategy) -> str | None:
        if not strategy.prebuild:
            return None
        path = strategy.dir / strategy.prebuild
        if not path.is_file():
            raise BuildError(f"{strategy.name}: prebuild script {path} not found")
        return path.read_text()

    def _compile(self, strategy: Strategy, base: str, src: Path, prebuild: str | None, image: str,
                 out: _CappedLog, timeout: int) -> None:
        """Run compile.sh in a container of the base image, offline and limited, and commit it as `image`."""
        b = self.cfg["build"]
        mem = int(b.get("memory_mb") or 6144)
        cpus = float(b.get("cpus") or 4.0)
        pids = int(b.get("pids") or 1024)
        runtime = self.cfg.get("docker", "runtime")      # e.g. runsc (gVisor); None = the daemon's default
        merge = strategy.source.merge
        command = compile_command(merge, prebuild is not None)
        out.write(f"[cw] compiling without network: {mem} MB, {cpus:g} CPUs, {pids} processes, at most "
                  f"{timeout} s\n[cw] $ {command}\n")
        container = None
        killed = threading.Event()

        def stop() -> None:
            killed.set()
            try:
                container.kill()
            except docker.errors.APIError:
                pass

        timer = threading.Timer(timeout, stop)
        timer.daemon = True
        try:
            container = self.client.containers.create(
                base, entrypoint=["bash", "-c", command], command=[],
                name=f"{self.cfg['docker']['prefix']}-build-{strategy.name}-{secrets.token_hex(3)}",
                network_mode="none", mem_limit=f"{mem}m", memswap_limit=f"{mem}m", nano_cpus=int(cpus * 1e9),
                pids_limit=pids, cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
                log_config=LogConfig(type="json-file", config={"max-size": f"{max(1, self.log_cap // 2 ** 20)}m",
                                                               "max-file": "1"}),
                labels={self.cfg["docker"]["label"]: f"build-{strategy.name}"},
                environment={"HOME": "/root", "CW_BUILD": strategy.name},
                **({"runtime": runtime} if runtime else {}))
            with tempfile.TemporaryFile() as tar:
                _tar_sources(src, "project" if merge else "src", prebuild, tar)
                tar.seek(0)
                container.put_archive("/", tar)
            container.start()
            timer.start()
            _follow(container, out)
            code = container.wait(timeout=timeout + 120).get("StatusCode", -1)
            timer.cancel()
            container.reload()
            if code != 0 and killed.is_set():       # a compile that ended just as the timer fired still counts
                out.write(f"[cw] stopped after {timeout} s\n")
                raise BuildError(f"the build exceeded {timeout} s and was stopped. The end of the build log "
                                 f"shows how far it got")
            if code != 0:
                if (container.attrs.get("State") or {}).get("OOMKilled"):
                    raise BuildError(f"the build ran out of memory ({mem} MB). The end of the build log shows "
                                     f"which step")
                raise BuildError(f"the build failed (exit {code}). The end of the build log shows why")
            repository, tag = image.rsplit(":", 1)
            self.client.api.commit(container.id, repository=repository, tag=tag,
                                   changes=['ENTRYPOINT ["bash", "/cw-run.sh"]', "CMD []"])
            out.write(f"[cw] compiled: {image}\n")
        except docker.errors.APIError as exc:
            raise BuildError(f"the build container failed: {exc.explanation or exc}") from None
        finally:
            timer.cancel()
            if container is not None:
                try:
                    container.remove(force=True)
                except docker.errors.APIError as exc:
                    log.warning("%s: removing the build container: %s", strategy.name, exc)

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

    def _g(self, repo: Path | None, *args: str) -> str:
        return _git(repo, *args, fsize=self.git_max_mb * 2 ** 20)

    def _git_fetch(self, repo: Path, ref: str | None) -> None:
        """Fetch `ref` (a branch, a tag or a full commit id; the default branch when None) one commit deep and
        check it out. A short commit id cannot be fetched by itself: then the branches come whole."""
        try:
            self._g(repo, "fetch", "--quiet", "--depth", "1", "--no-tags", "origin", "--end-of-options", ref or "HEAD")
            commit = "FETCH_HEAD"
        except BuildError as exc:
            if not (ref and SHORT_SHA_RE.match(ref)) or GIT_TOO_BIG_RE.search(str(exc)) \
                    or GIT_NO_ACCESS_RE.search(str(exc)):
                raise
            shallow = self._g(repo, "rev-parse", "--is-shallow-repository").strip() == "true"
            self._g(repo, "fetch", "--quiet", "--no-tags", *(["--unshallow"] if shallow else []), "origin")
            commit = self._g(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
        # `commit` is FETCH_HEAD or a full id from rev-parse: nothing here can pass for an option
        self._g(repo, "checkout", "--quiet", "--force", commit)
        self.last_commit = self._g(repo, "rev-parse", "HEAD").strip()

    def _fetch_source(self, strategy: Strategy) -> Path:
        src = strategy.source
        self.last_commit = None
        self.last_links = set()
        if src.dir:
            base = root = strategy.source_root()
            if not root.is_dir():
                raise BuildError(f"{strategy.name}: source dir {root} not found")
        else:
            url = check_git_url(src.git, self.git_hosts)
            repo = self.cfg.build_dir / "repos" / strategy.name
            if (repo / ".git").is_dir():
                try:
                    origin = self._g(repo, "remote", "get-url", "origin").strip()
                except BuildError:
                    origin = None
                if origin != url:
                    shutil.rmtree(repo)
            try:
                if not (repo / ".git").is_dir():
                    if repo.exists():
                        shutil.rmtree(repo)
                    repo.mkdir(parents=True)
                    log.info("%s: fetching %s", strategy.name, url)
                    self._g(repo, "init", "--quiet")
                    self._g(repo, "remote", "add", "origin", url)
                self._git_fetch(repo, src.ref)
                self.last_links = _git_links(repo)
            except BuildError as exc:
                raise git_failure(str(exc).replace(str(repo), "<clone>"), url, src.ref, self.git_max_mb) from None
            base = root = repo
        if src.path:
            root = root / src.path
            if not root.is_dir():
                raise BuildError(f"{strategy.name}: source.path {root} not found")
        if strategy.language:
            root = language_subdir(root, strategy.language)
        # a link on the way to the source directory (a clone made before links were checked out as files)
        if not root.resolve().is_relative_to(base.resolve()):
            raise BuildError(f"{strategy.name}: the source directory is a symbolic link out of the repository; "
                             f"links are not accepted, copy the files instead")
        return root

    @staticmethod
    def _copy_source(strategy: Strategy, root: Path, dst: Path, links: set[Path] | frozenset = frozenset(),
                     max_files: int | None = None, max_mb: int | None = None) -> None:
        """Copy the selected files, at most `max_files` of them and `max_mb` in all (the limits of a zip upload).
        A symbolic link (on disk, or recorded in git: `links`) is an error: a copy would carry whatever it
        points at, the server's secrets included, into the build."""
        include = strategy.source.include
        dst.mkdir(parents=True)
        files = size = 0
        for path in root.rglob("*"):
            rel = path.relative_to(root)
            if ".git" in rel.parts or "__pycache__" in rel.parts:
                continue
            if include and not any(fnmatch.fnmatch(str(rel), pat) for pat in include):
                continue
            if path.is_symlink() or path in links:
                raise BuildError(f"{strategy.name}: {rel} is a symbolic link; links are not accepted, "
                                 f"copy the file instead")
            target = dst / rel
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            files += 1
            size += path.stat().st_size
            if max_files and files > max_files:
                raise BuildError(f"{strategy.name}: the source has more than {max_files} files; trim it")
            if max_mb and size > max_mb * 2 ** 20:
                raise BuildError(f"{strategy.name}: the source is larger than {max_mb} MB; trim it")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target, follow_symlinks=False)
        if not any(dst.iterdir()):
            raise BuildError(f"{strategy.name}: no source files selected from {root} (include={include})")

    # Docker --------------------------------------------------------------
    def _image_exists(self, image: str) -> bool:
        try:
            self.client.images.get(image)
            return True
        except docker.errors.ImageNotFound:
            return False

    def _docker_build(self, ctx: Path, tag: str, out: _CappedLog, nocache: bool = False, timeout: int = 1200) -> None:
        """A Dockerfile build of trusted files (the engine, a base image), with the network."""
        error = None
        try:
            for chunk in self.client.api.build(path=str(ctx), tag=tag, rm=True, nocache=nocache, decode=True,
                                               timeout=timeout):
                if "stream" in chunk:
                    stream = ANSI_RE.sub("", chunk["stream"])
                    out.write(stream)
                    text = stream.rstrip()
                    if text:
                        log.info("  %s", text[:200])
                elif "error" in chunk:
                    error = ANSI_RE.sub("", chunk.get("errorDetail", {}).get("message") or chunk["error"])
                    out.write(f"ERROR: {error}\n")
        except Exception as exc:  # noqa: BLE001 - e.g. a read timeout on a silent build step
            error = f"{type(exc).__name__}: {exc}"
            out.write(f"ERROR: {error}\n")
        if error:
            raise BuildError(f"the build failed: {error}\nThe end of the build log shows why.")
