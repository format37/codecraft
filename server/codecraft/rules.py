"""The rules: the arena (ours, docs/arena.md, rendered with the numbers of config.yaml) and the game (the official
document, docs/doc-en.md). The web page /rules shows both with a table of contents; the agents' rules() tool and the
codecraft://rules resource return the same Markdown."""
from __future__ import annotations

import re

import jinja2

from .config import Config

ARENA_FILE = "arena.md"
GAME_FILE = "doc-en.md"

_env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False, keep_trailing_newline=True)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FORMAT_NAMES = {"Round1": "Round 1", "Round2": "Round 2", "Finals": "Finals"}


def _num(x) -> str:
    """1.0 -> '1', 0.5 -> '0.5'."""
    if x is None:
        return "no"
    f = float(x)
    return str(int(f)) if f == int(f) else str(f)


def numbers(cfg: Config) -> dict:
    """What the arena text states from config.yaml."""
    w, r, lim, sub, mm = cfg["worker"], cfg["rating"], cfg["limits"], cfg["submissions"], cfg["matchmaking"]
    leagues = cfg.get("leagues") or {}
    per_build = int(w.get("sandbox_games_per_build") or 0)
    daily = int(w.get("sandbox_daily_games") or 0)
    formats = {n: float(wt) for n, wt in cfg["formats"].items() if wt and float(wt) > 0}
    names = [_FORMAT_NAMES.get(n, n) for n in formats]
    if len(set(formats.values())) <= 1:
        formats_text = ", ".join(names[:-1]) + (" or " if len(names) > 1 else "") + names[-1] + ", with equal odds"
    else:
        formats_text = ", ".join(f"{name} (weight {_num(wt)})" for name, wt in zip(names, formats.values()))
    tick, cpu = lim.get("tick_timeout_sec"), lim.get("cpu_time_sec")
    limits_text = (f"{_num(tick)} s of real time to answer a tick" if tick else "no time limit per tick")
    limits_text += (f" and {_num(cpu)} s of CPU time for the whole game" if cpu else " and no CPU time limit")
    if tick == 1 and cpu == 40:
        limits_text += " (the 2020 limits)"
    cpus = lim.get("cpus") or 1
    limits_text += f", {int(lim['memory_mb'])} MB of memory for its container and {_num(cpus)} CPU core" + \
        ("s" if float(cpus) != 1 else "")
    points = cfg["points"]
    return {
        "base": str(cfg["auth"]["base_url"]).rstrip("/"),
        "initial": _num(r["initial"]), "k": _num(r["k"]), "k_min": _num(r.get("k_min", r["k"])),
        "half_life": _num(r.get("k_half_life_games") or 0), "sigma": _num(mm.get("rating_sigma", 200)),
        "formats_text": formats_text,
        "per_build": per_build, "daily": daily,
        "daily_text": "one rated game" if daily == 1 else f"{daily} rated games",
        "points_r": " / ".join(str(p) for p in points["Round1"]), "points_f": " / ".join(str(p) for p in points["Finals"]),
        "limits_text": limits_text,
        "min_players": int(leagues.get("min_players", 40)), "settled_games": int(leagues.get("settled_games", 30)),
        "versions_per_day": int(sub.get("per_user_per_day") or 20),
        "custom_per_day": int(sub.get("custom_games_per_day") or 50), "custom_queued": int(sub.get("custom_games_queued") or 3),
    }


def exists(cfg: Config) -> bool:
    return (cfg.root / "docs" / ARENA_FILE).is_file() and (cfg.root / "docs" / GAME_FILE).is_file()


def arena_markdown(cfg: Config) -> str:
    text = (cfg.root / "docs" / ARENA_FILE).read_text()
    text = re.sub(r"^<!--.*?-->\n", "", text, count=1, flags=re.S)     # the note for readers of the repository
    return _env.from_string(text).render(**numbers(cfg))


def game_markdown(cfg: Config) -> str:
    return (cfg.root / "docs" / GAME_FILE).read_text()


def rules_markdown(cfg: Config) -> str:
    return arena_markdown(cfg).rstrip() + "\n\n" + game_markdown(cfg)


def headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, title) of the Markdown headings outside code fences."""
    out, fenced = [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        m = None if fenced else HEADING_RE.match(line)
        if m:
            out.append((i, len(m[1]), m[2].replace("`", "")))
    return out


def section(text: str, needle: str) -> str | None:
    """The first section whose heading contains `needle` (case-insensitive), up to the next heading of its level or
    higher; None when there is none."""
    lines = text.splitlines()
    heads = headings(lines)
    needle = needle.strip().strip("#` ").lower()
    for n, (i, level, title) in enumerate(heads):
        if needle in title.lower():
            end = next((j for j, lv, _ in heads[n + 1:] if lv <= level), len(lines))
            return "\n".join(lines[i:end]).strip() + "\n"
    return None


def html(text: str) -> tuple[str, str]:
    """(body, table of contents) as HTML; the contents two levels deep, the headings with ids."""
    import markdown
    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc"],
                           extension_configs={"toc": {"toc_depth": "1-2"}})
    body = md.convert(text)
    return body, md.toc
