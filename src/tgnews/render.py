"""Draw Telugu news cards and carousels as 1080x1350 JPEGs.

Telugu needs complex text shaping, so everything is drawn with Pillow's RAQM layout
engine and the bundled Noto Sans Telugu font (SIL OFL, see assets/fonts/OFL.txt).
"""
from __future__ import annotations

import hashlib
import math
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

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


# Dark backgrounds with near-white text: contrast ratios are well above WCAG AA (4.5:1).
PALETTE: dict[str, Theme] = {
    "politics": Theme((20, 33, 61), (14, 22, 43), (255, 255, 255), (203, 213, 235)),
    "crime": Theme((59, 18, 24), (38, 10, 15), (255, 255, 255), (232, 205, 208)),
    "weather": Theme((12, 54, 82), (8, 36, 58), (255, 255, 255), (194, 222, 240)),
    "jobs": Theme((30, 50, 80), (20, 34, 56), (255, 255, 255), (206, 219, 238)),
    "farming": Theme((24, 60, 36), (15, 40, 24), (255, 255, 255), (206, 228, 211)),
    "sports": Theme((14, 62, 62), (8, 42, 42), (255, 255, 255), (196, 228, 228)),
    "education": Theme((48, 36, 84), (32, 24, 58), (255, 255, 255), (219, 211, 240)),
    "health": Theme((14, 64, 58), (8, 44, 40), (255, 255, 255), (196, 230, 225)),
    "business": Theme((52, 40, 20), (36, 27, 12), (255, 255, 255), (232, 220, 196)),
    "other": Theme((34, 38, 48), (22, 25, 33), (255, 255, 255), (209, 213, 222)),
}
DEFAULT_THEME = PALETTE["other"]


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


def _background(theme: Theme) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), theme.bg_top)
    if theme.bg_top != theme.bg_bottom:
        px = ImageDraw.Draw(img)
        for y in range(HEIGHT):
            t = y / (HEIGHT - 1)
            c = tuple(round(a + (b - a) * t) for a, b in zip(theme.bg_top, theme.bg_bottom))
            px.line([(0, y), (WIDTH, y)], fill=c)
    return img


def _credit_text(post: RewrittenPost) -> str:
    credit = post.source_credit.strip()
    return credit if credit.startswith("మూలం") else f"మూలం: {credit}"


def _draw_lines(draw, lines: list[str], font, x: int, y: int, line_h: int, fill) -> int:
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_h
    return y


def _frame(theme: Theme, brand: str, credit: str, page: str | None = None):
    """Draw background, brand, footer rule and credit. Returns (img, draw, top_y, bottom_y)."""
    img = _background(theme)
    draw = ImageDraw.Draw(img)
    inner_w = WIDTH - 2 * MARGIN
    # Brand, small, at the top.
    bfont = _font(32, True)
    bfont_h = int(32 * LINE_SPACING)
    brand_lines = _truncate_to_fit(draw, brand, bfont, inner_w - 160, 1) if _width(draw, brand, bfont) > inner_w - 160 else [brand]
    draw.text((MARGIN, MARGIN), brand_lines[0], font=bfont, fill=theme.muted)
    if page:
        pf = _font(30, False)
        draw.text((WIDTH - MARGIN - _width(draw, page, pf), MARGIN), page, font=pf, fill=theme.muted)
    top = MARGIN + bfont_h + 24
    # Footer: thin rule and source credit.
    cfont, cl, cline_h = fit_paragraphs(draw, [credit], inner_w, int(30 * LINE_SPACING) * 2, 30, 24, False)
    foot_h = len(cl[0]) * cline_h
    foot_y = HEIGHT - MARGIN - foot_h
    rule_y = foot_y - 24
    draw.line([(MARGIN, rule_y), (WIDTH - MARGIN - 1, rule_y)], fill=theme.muted, width=2)
    _draw_lines(draw, cl[0], cfont, MARGIN, foot_y, cline_h, theme.muted)
    return img, draw, top, rule_y - 40


def _theme(post: RewrittenPost) -> Theme:
    return PALETTE.get(post.topic, DEFAULT_THEME)


def _save(img: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=JPEG_QUALITY)
    return path


def _slug(post: RewrittenPost) -> str:
    raw = "\x1f".join([post.headline_te, *post.points_te, post.source_credit])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def _headline_slide(post, theme, brand, page=None) -> Image.Image:
    img, draw, top, bottom = _frame(theme, brand, _credit_text(post), page)
    font, wrapped, line_h = fit_paragraphs(
        draw, [post.headline_te], WIDTH - 2 * MARGIN, bottom - top, 84, 44, True
    )
    block_h = len(wrapped[0]) * line_h
    y = top + max(0, (bottom - top - block_h) // 2)
    _draw_lines(draw, wrapped[0], font, MARGIN, y, line_h, theme.text)
    return img


def render_card(post: RewrittenPost, out_dir, brand: str = DEFAULT_BRAND) -> list[Path]:
    """One 1080x1350 JPEG with brand, headline and source credit."""
    assert_shaping_available()
    img = _headline_slide(post, _theme(post), brand)
    return [_save(img, Path(out_dir) / f"card-{_slug(post)}.jpg")]


MAX_CAROUSEL_POINTS = 6  # 2 per slide, at most 3 point slides: total 3-5 slides


def _chunk_points(points: list[str]) -> list[list[str]]:
    points = points[:MAX_CAROUSEL_POINTS]
    return [points[i:i + 2] for i in range(0, len(points), 2)]


def render_carousel(post: RewrittenPost, out_dir, brand: str = DEFAULT_BRAND) -> list[Path]:
    """Headline slide, one slide per 1-2 key points, then a source credit slide (3-5 total)."""
    assert_shaping_available()
    points = [p for p in post.points_te if p.strip()]
    if not points:
        raise ValueError("carousel needs at least one key point")
    theme = _theme(post)
    chunks = _chunk_points(points)
    total = len(chunks) + 2
    slug = _slug(post)
    out = Path(out_dir)
    paths = [_save(_headline_slide(post, theme, brand, f"1/{total}"), out / f"carousel-{slug}-1.jpg")]
    inner_w = WIDTH - 2 * MARGIN
    n = 0
    for i, chunk in enumerate(chunks, start=2):
        img, draw, top, bottom = _frame(theme, brand, _credit_text(post), f"{i}/{total}")
        paras = []
        for p in chunk:
            n += 1
            paras.append(f"{n}. {p}")
        font, wrapped, line_h = fit_paragraphs(draw, paras, inner_w, bottom - top, 66, 40, True, gap=48)
        block_h = sum(len(w) for w in wrapped) * line_h + 48 * (len(wrapped) - 1)
        y = top + max(0, (bottom - top - block_h) // 2)
        for w in wrapped:
            y = _draw_lines(draw, w, font, MARGIN, y, line_h, theme.text) + 48
        paths.append(_save(img, out / f"carousel-{slug}-{i}.jpg"))
    # Source slide.
    img, draw, top, bottom = _frame(theme, brand, _credit_text(post), f"{total}/{total}")
    font, wrapped, line_h = fit_paragraphs(draw, [_credit_text(post)], inner_w, bottom - top, 64, 36, True)
    block_h = len(wrapped[0]) * line_h
    y = top + max(0, (bottom - top - block_h) // 2)
    _draw_lines(draw, wrapped[0], font, MARGIN, y, line_h, theme.text)
    paths.append(_save(img, out / f"carousel-{slug}-{total}.jpg"))
    return paths
