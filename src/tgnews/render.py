"""Draw Telugu news cards and carousels as 1080x1350 JPEGs.

Telugu needs complex text shaping, so everything is drawn with Pillow's RAQM layout
engine and the bundled Noto Sans Telugu font (SIL OFL, see assets/fonts/OFL.txt).
"""
from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, features

from tgnews.rewrite import RewrittenPost

WIDTH, HEIGHT = 1080, 1350
MARGIN = 96
JPEG_QUALITY = 90
DEFAULT_BRAND = "Telangana News"
FONT_PATH = Path(__file__).resolve().parent / "assets" / "fonts" / "NotoSansTelugu-VF.ttf"
ELLIPSIS = "…"
LINE_SPACING = 1.55  # Telugu stacks tall; keep generous leading


class ShapingUnavailable(RuntimeError):
    """Raised when Pillow was built without raqm, so Telugu would render broken."""


@dataclass(frozen=True)
class Theme:
    bg_top: tuple[int, int, int]
    bg_bottom: tuple[int, int, int]
    text: tuple[int, int, int]
    muted: tuple[int, int, int]
    accent: tuple[int, int, int] = (255, 193, 7)


# Dark backgrounds with near-white text: contrast ratios are well above WCAG AA (4.5:1).
PALETTE: dict[str, Theme] = {
    "politics": Theme((20, 33, 61), (14, 22, 43), (255, 255, 255), (203, 213, 235), (255, 183, 77)),
    "crime": Theme((59, 18, 24), (38, 10, 15), (255, 255, 255), (232, 205, 208), (255, 112, 112)),
    "weather": Theme((12, 54, 82), (8, 36, 58), (255, 255, 255), (194, 222, 240), (100, 200, 255)),
    "jobs": Theme((30, 50, 80), (20, 34, 56), (255, 255, 255), (206, 219, 238), (120, 190, 255)),
    "farming": Theme((24, 60, 36), (15, 40, 24), (255, 255, 255), (206, 228, 211), (140, 220, 120)),
    "sports": Theme((14, 62, 62), (8, 42, 42), (255, 255, 255), (196, 228, 228), (80, 220, 200)),
    "education": Theme((48, 36, 84), (32, 24, 58), (255, 255, 255), (219, 211, 240), (190, 160, 255)),
    "health": Theme((14, 64, 58), (8, 44, 40), (255, 255, 255), (196, 230, 225), (90, 225, 190)),
    "business": Theme((52, 40, 20), (36, 27, 12), (255, 255, 255), (232, 220, 196), (255, 200, 90)),
    "other": Theme((34, 38, 48), (22, 25, 33), (255, 255, 255), (209, 213, 222), (255, 193, 7)),
}
DEFAULT_THEME = PALETTE["other"]
TOPIC_LABEL = {
    "politics": "రాజకీయాలు", "crime": "క్రైమ్", "weather": "వాతావరణం", "jobs": "ఉద్యోగాలు",
    "farming": "వ్యవసాయం", "sports": "క్రీడలు", "education": "విద్య", "health": "ఆరోగ్యం",
    "business": "వ్యాపారం", "other": "వార్తలు",
}


def assert_shaping_available() -> None:
    if not features.check("raqm"):
        raise ShapingUnavailable(
            "Pillow was built without raqm (libraqm); Telugu text would render with "
            "broken glyphs. Install libraqm/fribidi/harfbuzz and a Pillow wheel that has it."
        )


@lru_cache(maxsize=64)
def _font(size: int, bold: bool) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONT_PATH), size, layout_engine=ImageFont.Layout.RAQM)
    # Variable font axes in file order: Weight, Width.
    font.set_variation_by_axes([700 if bold else 400, 100])
    return font


def _width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    # Shaped glyphs (conjuncts, vowel signs) can overhang their advance width; measure the
    # larger of the two so ink never crosses the margin box.
    right = draw.textbbox((0, 0), text, font=font)[2]
    return max(draw.textlength(text, font=font), right)


def _wrap(draw, text: str, font, max_w: int) -> list[str] | None:
    """Greedy word wrap by shaped pixel width; None if a single word is too wide."""
    lines: list[str] = []
    cur = ""
    for word in text.split():
        trial = f"{cur} {word}" if cur else word
        if _width(draw, trial, font) <= max_w:
            cur = trial
        elif cur and _width(draw, word, font) <= max_w:
            lines.append(cur)
            cur = word
        elif not cur:
            return None
        else:
            return None
    if cur:
        lines.append(cur)
    return lines


def _is_extender(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x0C00 <= cp <= 0x0C04 or 0x0C3E <= cp <= 0x0C56
        or ch in ("\u200c", "\u200d") or unicodedata.category(ch) in ("Mn", "Mc")
    )


def _clusters(text: str) -> list[str]:
    """Split into grapheme-ish clusters: a base plus its vowel signs/viramas/joiners,
    and a consonant following a virama (conjunct) stays attached."""
    out: list[str] = []
    for ch in text:
        if out and (_is_extender(ch) or out[-1][-1] == "\u0c4d"):
            out[-1] += ch
        else:
            out.append(ch)
    return out


def _truncate_to_fit(draw, text: str, font, max_w: int, max_lines: int) -> list[str]:
    words = text.split()
    for k in range(len(words) - 1, 0, -1):
        lines = _wrap(draw, " ".join(words[:k]) + ELLIPSIS, font, max_w)
        if lines is not None and len(lines) <= max_lines:
            return lines
    # First word alone is too long (or only one word): cut it at a cluster boundary.
    clusters = _clusters(words[0] if words else "")
    def attempt(n: int):
        lines = _wrap(draw, "".join(clusters[:n]) + ELLIPSIS, font, max_w)
        return lines if lines is not None and len(lines) <= max_lines else None

    lo, hi, best = 1, len(clusters) - 1, None  # binary search the longest prefix that fits
    while lo <= hi:
        mid = (lo + hi) // 2
        got = attempt(mid)
        if got is not None:
            best, lo = got, mid + 1
        else:
            hi = mid - 1
    if best is not None:
        return best
    return [ELLIPSIS]


def fit_paragraphs(
    draw, paragraphs: list[str], box_w: int, box_h: int, max_size: int, min_size: int,
    bold: bool, gap: int = 0,
) -> tuple[ImageFont.FreeTypeFont, list[list[str]], int]:
    """Largest font size (stepping down) at which all paragraphs fit the box.

    Returns (font, wrapped lines per paragraph, line height). If even min_size does not
    fit, paragraphs are truncated at a word boundary with an ellipsis.
    """
    for size in range(max_size, min_size - 1, -2):
        font = _font(size, bold)
        line_h = int(size * LINE_SPACING)
        wrapped = [_wrap(draw, p, font, box_w) for p in paragraphs]
        if any(w is None for w in wrapped):
            continue
        total = sum(len(w) for w in wrapped) * line_h + gap * (len(wrapped) - 1)
        if total <= box_h:
            return font, wrapped, line_h
    font = _font(min_size, bold)
    line_h = int(min_size * LINE_SPACING)
    avail_lines = max(1, (box_h - gap * (len(paragraphs) - 1)) // line_h)
    per_par = max(1, avail_lines // len(paragraphs))
    result = []
    for p in paragraphs:
        w = _wrap(draw, p, font, box_w)
        if w is None or len(w) > per_par:
            w = _truncate_to_fit(draw, p, font, box_w, per_par)
        result.append(w)
    return font, result, line_h


def _draw_lines(draw, lines: list[str], font, x: int, y: int, line_h: int, fill) -> int:
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_h
    return y


STYLES = ("glass", "contour")
PANEL = (MARGIN, 330, WIDTH - MARGIN, 1100)  # x0, y0, x1, y1: text panel shared by all slides
PANEL_PAD = 54
TEXT_TOP_OFFSET = 70  # text starts this far below the panel top (room for the accent line)


def _grain(img: Image.Image, sigma: float) -> Image.Image:
    """Film grain: gaussian noise centred on mid-grey added to every channel."""
    noise = Image.effect_noise(img.size, sigma).convert("RGB")
    return ImageChops.add(img.convert("RGB"), noise, scale=1.0, offset=-128)


def _gradient(top, bottom) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), top)
    px = ImageDraw.Draw(img)
    for y in range(HEIGHT):
        t = y / (HEIGHT - 1)
        px.line([(0, y), (WIDTH, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return img


def _lift(c, f):
    return tuple(min(255, int(v * f)) for v in c)


def _contours(draw, cx, cy, rings, step, alpha_major, alpha_minor, color, wobble):
    for k in range(1, rings + 1):
        pts = []
        for t in range(0, 361, 3):
            a = math.radians(t)
            r = k * step * (1 + 0.16 * math.sin(3 * a + k * 0.13 + wobble) + 0.09 * math.sin(5 * a - k * 0.21)
                            + 0.05 * math.sin(9 * a + k * 0.4))
            pts.append((cx + r * 1.25 * math.cos(a), cy + r * 1.05 * math.sin(a)))
        major = k % 5 == 0
        draw.line(pts + [pts[0]], fill=(*color, alpha_major if major else alpha_minor), width=3 if major else 2)


@lru_cache(maxsize=32)
def _decor(style: str, theme: Theme) -> Image.Image:
    """Textured background (no text, nothing that depends on the post). Cached per style+theme."""
    if style == "glass":
        base = _gradient(_lift(theme.bg_top, 1.5), _lift(theme.bg_bottom, 0.8)).convert("RGBA")
        glow = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.ellipse([560, -220, 1300, 520], fill=(*theme.accent, 140))
        gd.ellipse([-320, 900, 520, 1700], fill=(*_lift(theme.accent, 0.6), 110))
        gd.ellipse([300, 1000, 900, 1500], fill=(*_lift(theme.accent, 0.8), 60))
        base = Image.alpha_composite(base, glow.filter(ImageFilter.GaussianBlur(110)))
        return _grain(base, 14)
    base = _gradient(_lift(theme.bg_bottom, 0.55), _lift(theme.bg_bottom, 0.4)).convert("RGBA")
    lines = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lines)
    _contours(ld, 640, 470, 34, 34, 140, 70, theme.accent, 0.0)
    _contours(ld, 180, 1180, 17, 40, 90, 45, theme.accent, 1.0)
    return _grain(Image.alpha_composite(base, lines), 10)


def _panel_and_frame(style, theme, brand, topic, index, total) -> Image.Image:
    img = _decor(style, theme).copy().convert("RGBA")
    x0, y0, x1, y1 = PANEL
    if style == "glass":
        shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle([x0, y0 + 30, x1, y1 + 30], radius=48, fill=(0, 0, 0, 150))
        img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(36)))
        pan = Image.new("RGBA", img.size, (0, 0, 0, 0))
        pd = ImageDraw.Draw(pan)
        pd.rounded_rectangle([x0, y0, x1, y1], radius=48, fill=(255, 255, 255, 34), outline=(255, 255, 255, 110), width=2)
        pd.rounded_rectangle([x0 + 2, y0 + 2, x1 - 2, y0 + 90], radius=46, fill=(255, 255, 255, 20))
        pd.rounded_rectangle([x0 + PANEL_PAD, y0 + 46, x0 + PANEL_PAD + 120, y0 + 54], radius=4, fill=(*theme.accent, 255))
        img = Image.alpha_composite(img, pan)
    img = img.convert("RGB")
    draw = ImageDraw.Draw(img)
    if style == "contour":
        draw.rectangle([x0 + 16, y0 + 16, x1 + 16, y1 + 16], fill=(0, 0, 0))
        draw.rectangle([x0, y0, x1, y1], fill=theme.accent)
    inner_w = WIDTH - 2 * MARGIN
    bfont = _font(34, True)
    chip_font = _font(30, True)
    label = TOPIC_LABEL.get(topic, TOPIC_LABEL["other"])
    chip_w = int(_width(draw, label, chip_font)) + 48
    chip_h = 58
    logo_w = _draw_map_logo(draw, MARGIN, MARGIN - 4, LOGO_H, theme.text, theme.bg_bottom)
    brand_x = MARGIN + logo_w + 22
    brand_max = inner_w - chip_w - 32 - logo_w - 22
    brand_lines = _truncate_to_fit(draw, brand, bfont, brand_max, 1) if _width(draw, brand, bfont) > brand_max else [brand]
    draw.text((brand_x, MARGIN + 8), brand_lines[0], font=bfont, fill=theme.text)
    cx = WIDTH - MARGIN - chip_w
    draw.rounded_rectangle([cx, MARGIN, cx + chip_w - 1, MARGIN + chip_h], radius=chip_h // 2, fill=theme.accent)
    draw.text((cx + 24, MARGIN + 4), label, font=chip_font, fill=theme.bg_bottom)
    bar_y = HEIGHT - MARGIN - 20
    if total > 1:
        gap, seg_h = 12, 8
        seg_w = (inner_w - gap * (total - 1)) // total
        for i in range(total):
            sx = MARGIN + i * (seg_w + gap)
            draw.rounded_rectangle([sx, bar_y, sx + seg_w - 1, bar_y + seg_h], radius=4,
                                   fill=theme.accent if i <= index else theme.muted)
    else:
        draw.rounded_rectangle([MARGIN, bar_y, MARGIN + 160, bar_y + 8], radius=4, fill=theme.accent)
    return img


MAP_PATH = Path(__file__).resolve().parent / "assets" / "telangana.json"
LOGO_H = 64  # height of the map icon in the header


@lru_cache(maxsize=1)
def _map_data() -> dict:
    return json.loads(MAP_PATH.read_text(encoding="utf-8"))


def _draw_map_logo(draw: ImageDraw.ImageDraw, x: int, y: int, h: int, fill, dot) -> int:
    """Telangana outline (public-domain Natural Earth boundary) with a dot at Hyderabad.
    Returns the icon width."""
    data = _map_data()
    ring = data["ring"]
    lons, lats = [p[0] for p in ring], [p[1] for p in ring]
    lat0 = sum(lats) / len(lats)
    kx = math.cos(math.radians(lat0))  # shrink longitude so the shape is not stretched
    minx, maxx, miny, maxy = min(lons), max(lons), min(lats), max(lats)
    scale = h / (maxy - miny)
    w = int((maxx - minx) * kx * scale)

    def proj(lon, lat):
        return (x + (lon - minx) * kx * scale, y + (maxy - lat) * scale)

    draw.polygon([proj(*p) for p in ring], fill=fill)
    hx, hy = proj(*data["hyderabad"])
    r = max(4, h // 11)
    draw.ellipse([hx - r - 2, hy - r - 2, hx + r + 2, hy + r + 2], fill=fill)
    draw.ellipse([hx - r, hy - r, hx + r, hy + r], fill=dot)
    return w


@lru_cache(maxsize=64)
def _base_cached(style, theme, brand, topic, index, total) -> Image.Image:
    return _panel_and_frame(style, theme, brand, topic, index, total)


def base_slide(post: RewrittenPost, brand: str, index: int = 0, total: int = 1) -> Image.Image:
    """The textured slide with panel, brand, chip and progress, but no headline/points text."""
    return _base_cached(style_for(post), _theme(post), brand, post.topic, index, total).copy()


def _theme(post: RewrittenPost) -> Theme:
    return PALETTE.get(post.topic, DEFAULT_THEME)


def _slug(post: RewrittenPost) -> str:
    raw = "\x1f".join([post.headline_te, *post.points_te, post.source_credit])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def style_for(post: RewrittenPost) -> str:
    """Visual style, chosen deterministically (about half and half) from the post's content."""
    return STYLES[int(_slug(post), 16) % len(STYLES)]


def _save(img: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=JPEG_QUALITY)
    return path


def _text_color(style: str, theme: Theme):
    return theme.text if style == "glass" else (12, 20, 30)


def _text_box():
    x0, y0, x1, y1 = PANEL
    return x0 + PANEL_PAD, y0 + TEXT_TOP_OFFSET, x1 - x0 - 2 * PANEL_PAD, (y1 - PANEL_PAD) - (y0 + TEXT_TOP_OFFSET)


def _headline_slide(post, brand, index=0, total=1) -> Image.Image:
    style, theme = style_for(post), _theme(post)
    img = base_slide(post, brand, index, total)
    draw = ImageDraw.Draw(img)
    tx, ty, tw, th = _text_box()
    font, wrapped, line_h = fit_paragraphs(draw, [post.headline_te], tw, th, 84, 44, True)
    block_h = len(wrapped[0]) * line_h
    _draw_lines(draw, wrapped[0], font, tx, ty + max(0, (th - block_h) // 2), line_h, _text_color(style, theme))
    return img


def render_card(post: RewrittenPost, out_dir, brand: str = DEFAULT_BRAND) -> list[Path]:
    """One 1080x1350 JPEG with brand, topic chip and headline."""
    assert_shaping_available()
    return [_save(_headline_slide(post, brand), Path(out_dir) / f"card-{_slug(post)}.jpg")]


MAX_CAROUSEL_POINTS = 6  # 2 per slide, at most 3 point slides: total 2-4 slides


def _chunk_points(points: list[str]) -> list[list[str]]:
    points = points[:MAX_CAROUSEL_POINTS]
    return [points[i:i + 2] for i in range(0, len(points), 2)]


BADGE_D, BADGE_GAP = 72, 30


def render_carousel(post: RewrittenPost, out_dir, brand: str = DEFAULT_BRAND) -> list[Path]:
    """Headline slide, then one slide per 1-2 key points (2-4 slides total)."""
    assert_shaping_available()
    points = [p for p in post.points_te if p.strip()]
    if not points:
        raise ValueError("carousel needs at least one key point")
    style, theme = style_for(post), _theme(post)
    chunks = _chunk_points(points)
    total = len(chunks) + 1
    slug = _slug(post)
    out = Path(out_dir)
    paths = [_save(_headline_slide(post, brand, 0, total), out / f"carousel-{slug}-1.jpg")]
    tx, ty, tw, th = _text_box()
    text_w = tw - BADGE_D - BADGE_GAP
    num_font = _font(40, True)
    badge_fill, badge_text = (theme.accent, theme.bg_bottom) if style == "glass" else ((12, 20, 30), theme.accent)
    n = 0
    for i, chunk in enumerate(chunks, start=1):
        img = base_slide(post, brand, i, total)
        draw = ImageDraw.Draw(img)
        gap = 48
        font, wrapped, line_h = fit_paragraphs(draw, list(chunk), text_w, th, 56, 34, True, gap=gap)
        block_h = sum(len(w) for w in wrapped) * line_h + gap * (len(wrapped) - 1)
        y = ty + max(0, (th - block_h) // 2)
        for w in wrapped:
            n += 1
            draw.ellipse([tx, y + 6, tx + BADGE_D, y + 6 + BADGE_D], fill=badge_fill)
            nt = str(n)
            draw.text((tx + BADGE_D / 2 - _width(draw, nt, num_font) / 2, y + 6 + BADGE_D / 2 - 30), nt,
                      font=num_font, fill=badge_text)
            y = _draw_lines(draw, w, font, tx + BADGE_D + BADGE_GAP, y, line_h, _text_color(style, theme)) + gap
        paths.append(_save(img, out / f"carousel-{slug}-{i + 1}.jpg"))
    return paths
