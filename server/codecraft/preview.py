"""Link-preview images (Open Graph): the game field at a tick plus the scoreboard as a 1200x630 PNG,
rendered from the decoded replay with Pillow. The web app serves and caches them next to
`replay.dat`; the worker pre-renders the final one when a game finishes, so a link pasted into
Telegram, Slack or Discord shows the board at once."""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont

from . import replay as R

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 1200, 630            # the standard Open Graph size (1.91:1)
KEYFRAME = 25                        # ticks other than the last are rounded down to this
PLAYER_COLORS = ["#2f7df6", "#e5484d", "#30a46c", "#f5a524"]   # as in the viewer (sim-cpp/viewer)
RESOURCE, NEUTRAL = "#8a9bb0", "#555555"
BG, FIELD, LINE, FG, MUTED = "#101418", "#0b0e12", "#232a34", "#e6e8ec", "#9aa3b2"
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    path = FONT_DIR / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        try:
            return ImageFont.load_default(size)
        except TypeError:   # Pillow < 10.1
            return ImageFont.load_default()


def site_host(base_url: str | None) -> str:
    """'codecraft.day' from the configured base URL (footer of the image)."""
    if not base_url:
        return ""
    return urlparse(base_url).netloc or base_url


def state_at(replay: R.Replay, tick: int | None = None):
    """(tick, players, entities) at `tick`; the last state when `tick` is None or beyond the end."""
    last = None
    for t, players, entities, _ in replay.states():
        last = (t, players, entities)
        if tick is not None and t >= tick:
            break
    return last


def _fit(d: ImageDraw.ImageDraw, text: str, font, width: float) -> str:
    if d.textlength(text, font=font) <= width:
        return text
    while text and d.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def render(replay: R.Replay, labels: list[str], *, tick: int | None = None, heading: str = "",
           footer: str = "") -> Image.Image:
    """Draw the field at `tick` (default: the last one) with the scoreboard on the right.
    `labels` are the player names by seat (player index)."""
    t, players, entities = state_at(replay, tick)
    size = replay.initial.map_size
    props = {R.ENTITY_TYPES[k]: v for k, v in replay.initial.entity_properties.items()}
    index_of = {pid: p[1] for pid, p in players.items()}

    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    d = ImageDraw.Draw(img)
    pad = 35
    cell = max(1, (HEIGHT - 2 * pad) // size)
    fw = cell * size
    x0, y0 = pad, (HEIGHT - fw) // 2
    d.rectangle([x0, y0, x0 + fw, y0 + fw], fill=FIELD, outline=LINE)
    for i in range(10, size, 10):
        d.line([x0 + i * cell, y0, x0 + i * cell, y0 + fw], fill=LINE)
        d.line([x0, y0 + i * cell, x0 + fw, y0 + i * cell], fill=LINE)

    def color(e) -> str:
        type_ = R.ENTITY_TYPES[e[2]]
        if type_ == "RESOURCE":
            return RESOURCE
        if e[1] is None:
            return NEUTRAL
        return PLAYER_COLORS[index_of.get(e[1], 0) % len(PLAYER_COLORS)]

    def entity_size(e) -> int:
        return int(props.get(R.ENTITY_TYPES[e[2]], {}).get("size") or 1)

    # buildings first, units on top of them
    for e in sorted(entities.values(), key=lambda e: -entity_size(e)):
        type_, s = R.ENTITY_TYPES[e[2]], entity_size(e)
        px = x0 + e[3] * cell
        py = y0 + (size - e[4] - s) * cell          # y grows upwards, as in the official viewer
        box = [px, py, px + s * cell - 1, py + s * cell - 1]
        c = color(e)
        if type_ == "RESOURCE":
            d.rectangle([px + 1, py + 1, px + cell - 2, py + cell - 2], fill=c)
        elif s == 1:
            d.ellipse(box, fill=c)
            if type_ == "RANGED_UNIT" and cell >= 5:
                cx, cy = px + cell // 2, py + cell // 2
                d.rectangle([cx - 1, cy - 1, cx, cy], fill="#ffffff")
        else:
            d.rectangle(box, fill=c, outline="#000000")
            max_hp = props.get(type_, {}).get("max_health") or e[5]
            if e[5] < max_hp and s * cell >= 10:
                w = s * cell - 4
                d.rectangle([px + 2, py + s * cell - 4, px + 2 + w, py + s * cell - 2], fill="#000000")
                d.rectangle([px + 2, py + s * cell - 4, px + 2 + int(w * e[5] / max_hp), py + s * cell - 2],
                            fill="#4ade80")

    # scoreboard
    left = x0 + fw + pad
    width = WIDTH - pad - left
    y = y0 + 4
    if heading:
        d.text((left, y), _fit(d, heading, _font(38, True), width), fill=FG, font=_font(38, True))
        y += 54
    sub = f"tick {t}" + (f" of {replay.tick_count}" if replay.tick_count else "") + f" · {size}×{size} map"
    d.text((left, y), sub, fill=MUTED, font=_font(20))
    y += 48
    ordered = sorted(players.values(), key=lambda p: p[1])
    ranks = {p[0]: i + 1 for i, p in enumerate(sorted(ordered, key=lambda p: -p[2]))}
    f_name, f_score, f_place = _font(27, True), _font(27), _font(20)
    for p in ordered:
        idx = p[1]
        c = PLAYER_COLORS[idx % len(PLAYER_COLORS)]
        d.rounded_rectangle([left, y + 6, left + 22, y + 28], radius=4, fill=c)
        label = labels[idx] if idx < len(labels) else f"player {idx + 1}"
        score = f"{p[2]:,}".replace(",", " ")
        place = f"#{ranks[p[0]]}"
        sw = d.textlength(score, font=f_score)
        pw = d.textlength(place, font=f_place)
        d.text((left + 34, y), _fit(d, label, f_name, width - 34 - sw - pw - 40), fill=FG, font=f_name)
        d.text((left + width - pw, y + 6), place, fill=MUTED, font=f_place)
        d.text((left + width - pw - 18 - sw, y), score, fill=FG, font=f_score)
        y += 48
    if footer:
        f = _font(18)
        d.text((left, y0 + fw - 24), footer, fill=MUTED, font=f)
    return img


def cache_path(gdir: Path, tick: int | None = None) -> Path:
    return gdir / ("preview.png" if tick is None else f"preview-{tick}.png")


def ensure(gdir: Path, labels: list[str], game_id: int, fmt: str, footer: str = "",
           tick: int | None = None) -> Path:
    """Return the cached preview for the game, rendering it when missing or older than the replay.
    A `tick` at or past the last tick means the final state (`preview.png`)."""
    raw = R.replay_file(gdir)
    if tick is not None:
        tick = max(0, (tick // KEYFRAME) * KEYFRAME)
    path = cache_path(gdir, tick)
    if raw is None:         # a replay the hub no longer keeps: the final image stays
        if path.exists() and tick is None:
            return path
        raise FileNotFoundError(f"game {game_id} has no replay here")
    if path.exists() and path.stat().st_mtime >= raw.stat().st_mtime:
        return path
    replay = R.load(raw)
    if tick is not None and tick >= replay.tick_count:
        tick, path = None, cache_path(gdir)
        if path.exists() and path.stat().st_mtime >= raw.stat().st_mtime:
            return path
    img = render(replay, labels, tick=tick, heading=f"Game #{game_id} · {fmt}", footer=footer)
    fd, tmp_name = tempfile.mkstemp(dir=gdir, prefix=f".{path.stem}.", suffix=".tmp")   # one per request
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            img.save(fh, format="PNG", optimize=True)
        os.chmod(tmp, 0o644)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path
