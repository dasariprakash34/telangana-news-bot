"""Turn a rewritten post into a vertical Telugu-voice Reel (MP4, 1080x1920).

The reel reuses the carousel slides (same look as the image posts): each slide is placed on a
blurred 9:16 background, read aloud by a free neural Telugu voice (edge-tts, no key), and slowly
zoomed with ffmpeg. Anything that goes wrong here (no ffmpeg, voice service unreachable) raises
`ReelUnavailable`, and the pipeline falls back to the normal image post, so a reel can never stop
a story from being posted.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from PIL import Image, ImageFilter

from .render import DEFAULT_BRAND, _chunk_points, _slug, render_carousel
from .rewrite import RewrittenPost

REEL_W, REEL_H = 1080, 1920
FPS = 24
TAIL_SECONDS = 0.4  # short pause after each slide's sentence
MAX_REEL_SECONDS = 75.0  # Instagram Reels allow far longer; a news reel should be short anyway
MAX_ZOOM = 1.06
log = logging.getLogger(__name__)
VOICE_ATTEMPTS, VOICE_BACKOFF = 3, 3.0  # seconds: wait 3 s, then 6 s


class ReelUnavailable(RuntimeError):
    """A reel could not be made (missing tool, voice failure, too long). Messages are safe to log."""


# -- narration ----------------------------------------------------------------
def narration(post: RewrittenPost, require_credit: bool = True) -> list[str]:
    """One spoken text per carousel slide, in slide order: the headline, then each slide's 1-2 key
    points. The source credit is read at the end when credit is required."""
    points = [p.strip() for p in post.points_te if p.strip()]
    if not points:
        raise ReelUnavailable("a reel needs at least one key point")
    texts = [post.headline_te.strip()]
    texts += [". ".join(chunk) for chunk in _chunk_points(points)]
    credit = (post.source_credit or "").strip()
    if require_credit and credit:
        texts[-1] = f"{texts[-1]}. మూలం: {credit}"
    return texts


# -- tools --------------------------------------------------------------------
def _tool(name: str) -> str:
    override = os.environ.get(name.upper())  # FFMPEG / FFPROBE
    found = override if override and Path(override).is_file() else shutil.which(name)
    if not found:
        raise ReelUnavailable(f"{name} is not installed")
    return found


def _run(cmd: list[str]) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReelUnavailable(f"{Path(cmd[0]).name} could not run ({type(exc).__name__})") from None
    if res.returncode != 0:
        raise ReelUnavailable(f"{Path(cmd[0]).name} failed (exit {res.returncode})")
    return res.stdout


def edge_tts_voice(text: str, voice: str, rate: str, path: Path,
                   sleep: Callable[[float], None] = time.sleep) -> list[tuple[float, float, str]]:
    """Write `text` as an MP3 with a Microsoft Edge neural voice (free, unofficial service) and
    return the spoken words as (start seconds, duration seconds, word), so on-screen text can be
    timed exactly to the voice."""
    try:
        import edge_tts
    except ImportError:
        raise ReelUnavailable("edge-tts is not installed") from None

    async def go() -> list[tuple[float, float, str]]:
        try:
            comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
        except TypeError:  # older edge-tts: word boundaries are the default
            comm = edge_tts.Communicate(text, voice, rate=rate)
        audio, words = bytearray(), []
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append((chunk["offset"] / 1e7, chunk["duration"] / 1e7, chunk["text"]))
        Path(path).write_bytes(bytes(audio))
        return words

    last = "unknown"
    for attempt in range(VOICE_ATTEMPTS):
        if attempt:
            sleep(VOICE_BACKOFF * attempt)  # the free service sometimes answers "no audio" under load
        try:
            words = asyncio.run(go())
        except Exception as exc:  # network, voice name, empty audio...
            last = type(exc).__name__
            continue
        if Path(path).is_file() and Path(path).stat().st_size > 0:
            return words
        last = "empty audio"
    raise ReelUnavailable(f"voice generation failed ({last}) after {VOICE_ATTEMPTS} tries")


# -- frames -------------------------------------------------------------------
def vertical_frame(slide: Path, out: Path) -> Path:
    """Place a 4:5 slide on a darkened, blurred 9:16 background (JPEG)."""
    with Image.open(slide) as src:
        src = src.convert("RGB")
        scale = max(REEL_W / src.width, REEL_H / src.height)
        bg = src.resize((math.ceil(src.width * scale), math.ceil(src.height * scale)))
        left, top = (bg.width - REEL_W) // 2, (bg.height - REEL_H) // 2
        bg = bg.crop((left, top, left + REEL_W, top + REEL_H)).filter(ImageFilter.GaussianBlur(36))
        bg = Image.blend(bg, Image.new("RGB", bg.size, (0, 0, 0)), 0.45)
        fit = REEL_W / src.width
        front = src.resize((REEL_W, round(src.height * fit)))
        bg.paste(front, (0, (REEL_H - front.height) // 2))
    out.parent.mkdir(parents=True, exist_ok=True)
    bg.save(out, "JPEG", quality=90)
    return out


# -- the reel -----------------------------------------------------------------
def render_reel(post: RewrittenPost, out_dir, brand: str = DEFAULT_BRAND, *, voice: str,
                rate: str, require_credit: bool = True, style: str = "animated",
                tts: Callable[[str, str, str, Path], None] = edge_tts_voice,
                run: Callable[[list[str]], str] = _run,
                render_slides: Callable = render_carousel,
                animated: Callable | None = None) -> list[Path]:
    """Render one MP4 reel for `post` into `out_dir`; returns [the mp4]. Raises ReelUnavailable.

    style "animated" (default): motion background, sliding cards and a 3D card scene, drawn in
    headless Chrome (reel_anim.py). If Chrome or the 3D page is unavailable it falls back to the
    plain "slides" style (the carousel slides with a slow zoom), which needs only ffmpeg."""
    if style == "animated":
        from .reel_anim import render_animated  # lazy: reel_anim imports this module

        try:
            return (animated or render_animated)(post, out_dir, brand, voice=voice, rate=rate,
                                                 require_credit=require_credit, tts=tts, run=run)
        except ReelUnavailable as exc:
            log.warning("animated reel unavailable (%s); making the slideshow reel instead", exc)
    return _render_slideshow(post, out_dir, brand, voice=voice, rate=rate,
                             require_credit=require_credit, tts=tts, run=run,
                             render_slides=render_slides)


def _render_slideshow(post: RewrittenPost, out_dir, brand: str, *, voice: str, rate: str,
                      require_credit: bool, tts, run, render_slides) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ffmpeg, ffprobe = _tool("ffmpeg"), _tool("ffprobe")
    texts = narration(post, require_credit)
    slides = render_slides(post, out / "slides", brand)
    if len(slides) != len(texts):
        raise ReelUnavailable("slide and narration counts differ")
    slug = _slug(post)
    segments: list[Path] = []
    total = 0.0
    for i, (slide, text) in enumerate(zip(slides, texts), start=1):
        frame = vertical_frame(Path(slide), out / f"frame-{slug}-{i}.jpg")
        mp3 = out / f"voice-{slug}-{i}.mp3"
        tts(text, voice, rate, mp3)
        try:
            seconds = float(run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", str(mp3)]).strip())
        except ValueError:
            raise ReelUnavailable("could not read the voice length") from None
        length = seconds + TAIL_SECONDS
        total += length
        if total > MAX_REEL_SECONDS:
            raise ReelUnavailable(f"reel would be longer than {MAX_REEL_SECONDS:.0f} seconds")
        frames = math.ceil(length * FPS)
        zoom = (f"zoompan=z='min(1+0.0004*on,{MAX_ZOOM})':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d={frames}:s={REEL_W}x{REEL_H}:fps={FPS},format=yuv420p")
        seg = out / f"seg-{slug}-{i}.mp4"
        run([ffmpeg, "-y", "-v", "error", "-i", str(frame), "-i", str(mp3),
             "-filter_complex", f"[0:v]{zoom}[v];[1:a]apad=pad_dur={TAIL_SECONDS}[a]",
             "-map", "[v]", "-map", "[a]", "-t", f"{length:.2f}", "-c:v", "libx264",
             "-preset", "veryfast", "-crf", "28", "-c:a", "aac", "-b:a", "96k", "-ar", "44100",
             str(seg)])
        segments.append(seg)
    listing = out / f"segments-{slug}.txt"
    listing.write_text("".join(f"file '{s.resolve()}'\n" for s in segments), encoding="utf-8")
    final = out / f"reel-{slug}.mp4"
    run([ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c", "copy", "-movflags", "+faststart", str(final)])
    return [final]
