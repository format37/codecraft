"""Strategy submissions: a zip upload or a git URL becomes a `strategy_versions` row that the worker builds."""
from __future__ import annotations

import io
import logging
import re
import shutil
import zipfile
from pathlib import Path

from .builder import BuildError, check_git_url
from .config import Config
from .db import DB
from .strategies import NAME_RE

log = logging.getLogger("codecraft.submit")

RESERVED = {"empty", "quickstart", "new", "admin", "me", "api", "static"}
REF_RE = re.compile(r"^[\w./@^~-]{1,120}$")


class SubmissionError(ValueError):
    pass


def check_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name) or name in RESERVED:
        raise SubmissionError("strategy name: 1-41 chars of [a-z0-9_-], starting with a letter or digit")
    return name


def extract_zip(data: bytes, dest: Path, max_files: int, max_unpacked_mb: int) -> int:
    """Unpack an uploaded archive into `dest`, refusing anything that would escape it. If the archive
    holds a single top-level directory, its contents become the root. Returns the number of files."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SubmissionError("the upload is not a zip archive")
    members = [m for m in zf.infolist() if not m.is_dir()]
    members = [m for m in members if not m.filename.startswith("__MACOSX/") and "/.git/" not in f"/{m.filename}"]
    if not members:
        raise SubmissionError("the archive is empty")
    if len(members) > max_files:
        raise SubmissionError(f"the archive has more than {max_files} files")
    total = sum(m.file_size for m in members)
    if total > max_unpacked_mb * 1024 * 1024:
        raise SubmissionError(f"the archive unpacks to more than {max_unpacked_mb} MB")
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    for m in members:
        name = m.filename.replace("\\", "/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if not parts or ".." in parts or name.startswith("/"):
            raise SubmissionError(f"unsafe path in the archive: {m.filename!r}")
        target = (root / Path(*parts)).resolve()
        if root not in target.parents:
            raise SubmissionError(f"unsafe path in the archive: {m.filename!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(m) as src, target.open("wb") as out:
            shutil.copyfileobj(src, out)
    # a single wrapping directory (what "compress folder" produces) is unwrapped
    entries = list(root.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        tmp = root.parent / (root.name + ".unwrap")
        inner.rename(tmp)
        shutil.rmtree(root)
        tmp.rename(root)
    return len(members)


def submit(cfg: Config, db: DB, *, name: str, language: str, user: dict | None, zip_data: bytes | None = None,
           git_url: str | None = None, git_ref: str | None = None, gpu: bool = False, merge: bool = False,
           include: list[str] | None = None, note: str | None = None, title: str | None = None,
           enforce_quota: bool = True) -> int:
    """Create (or extend) a strategy and queue a new version. Returns the version id."""
    name = check_name(name)
    sub = cfg["submissions"]
    if language not in cfg.languages():
        raise SubmissionError(f"unknown language {language!r}; available: {', '.join(cfg.languages())}")
    if (gpu or language.endswith("-cuda")) and not cfg.gpu_ok:
        raise SubmissionError("GPU strategies are disabled on this server")
    user_id = user["id"] if user else None
    row = db.strategy(name)
    if row is not None:
        if row["kind"] == "seed" and not (user and user.get("role") == "admin"):
            raise SubmissionError(f"{name!r} is a server-managed strategy")
        if row["owner_id"] != user_id and not (user and user.get("role") == "admin"):
            raise SubmissionError(f"{name!r} belongs to another user")
        if row["builtin"]:
            raise SubmissionError(f"{name!r} is a built-in bot")
    if enforce_quota and user is not None and user.get("role") != "admin":
        pending = db.user_pending_version_rows(user_id)
        if len(pending) >= int(sub.get("per_user_queued") or 1):
            waiting = ", ".join(f"{p['strategy']} v{p['number']} (version id {p['id']}, {p['status']})" for p in pending)
            raise SubmissionError(f"you already have a version waiting to be built: {waiting}; wait for it to finish")
        if db.user_versions_today(user_id) >= int(sub.get("per_user_per_day") or 20):
            raise SubmissionError("daily submission limit reached")
    if zip_data is not None:
        if len(zip_data) > int(sub.get("max_zip_mb") or 20) * 1024 * 1024:
            raise SubmissionError(f"the archive is larger than {sub.get('max_zip_mb')} MB")
        source_kind, source, ref = "zip", None, None
    elif git_url:
        if not sub.get("allow_git", True):
            raise SubmissionError("git submissions are disabled")
        try:
            git_url = check_git_url(git_url)
        except BuildError as exc:
            raise SubmissionError(str(exc))
        git_ref = (git_ref or "").strip() or None
        if git_ref and not REF_RE.match(git_ref):
            raise SubmissionError("bad git ref")
        source_kind, source, ref = "git", git_url, git_ref
    else:
        raise SubmissionError("upload a zip archive or give a git URL")
    if row is None:
        db.create_user_strategy(name, user_id, (title or name).strip()[:80], language, gpu,
                                float(cfg["rating"]["initial"]))
    version_id = db.create_version(name, user_id, source_kind, source, ref, language, gpu, merge, include,
                                   (note or "").strip()[:200] or None)
    if zip_data is not None:
        version = db.version(version_id)
        rel = Path("submissions") / name / f"v{version['number']}"
        dest = cfg.data_dir / rel
        if dest.exists():
            shutil.rmtree(dest)
        (dest).mkdir(parents=True)
        (dest / "source.zip").write_bytes(zip_data)
        try:
            n = extract_zip(zip_data, dest / "src", int(sub.get("max_files") or 2000),
                            int(sub.get("max_unpacked_mb") or 200))
        except SubmissionError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            with db.tx() as c:   # a bad archive leaves no trace: no version, and no strategy if it was new
                c.execute("DELETE FROM strategy_versions WHERE id=?", (version_id,))
                if row is None:
                    c.execute("DELETE FROM strategies WHERE name=?", (name,))
            raise
        with db.tx() as c:
            c.execute("UPDATE strategy_versions SET source=? WHERE id=?", (str(rel / "src"), version_id))
        log.info("submission %s v%d: %d files", name, version["number"], n)
    return version_id
