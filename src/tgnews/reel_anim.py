"""Animated reel renderer: bokeh motion background, sliding glass cards, a 3D card scene, in the
post's topic colours, drawn frame by frame in headless Chrome and encoded with ffmpeg.

`anim.html` (next to the bundled three.js and the Telugu font) exposes `setup(spec, meta)` and
`draw(t, duration)`. This module stages those files in a temp directory, drives one Chrome over the
DevTools protocol, takes a JPEG per frame, and pipes the frames to ffmpeg together with the voice.
Chrome shapes Telugu itself, so no Pillow/raqm is needed here. Anything that goes wrong raises
`ReelUnavailable`; `reel.render_reel` then falls back to the plain slideshow reel.
"""
from __future__ import annotations

import base64
import json
import math
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

from .reel import MAX_REEL_SECONDS, TAIL_SECONDS, ReelUnavailable, _chunk_points, _run, _tool, edge_tts_voice
from .render import DEFAULT_THEME, PALETTE, TOPIC_LABEL, FONT_PATH, _slug
from .rewrite import RewrittenPost

ASSETS = Path(__file__).resolve().parent / "assets" / "reel"
FPS = 24
JPEG_QUALITY = 85
# A scene lasts at least as long as its animation needs, even when the sentence read over it is short
# (the 3D cards take about 3.5 s to fly in and line up).
MIN_THREE_SECONDS, MIN_STEPS_SECONDS, MIN_TITLE_SECONDS = 4.6, 3.4, 3.0
CTA = "మరిన్ని తెలంగాణ వార్తల కోసం ఫాలో అవ్వండి"
_CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
_MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _hex(rgb) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


# -- scenes -------------------------------------------------------------------------------------
def headline_size(text: str) -> int:
    return 84 if len(text) <= 40 else 70 if len(text) <= 80 else 58


def scenes_for(post: RewrittenPost, brand: str, require_credit: bool = True) -> list[dict]:
    """The reel as scenes, each with the text on screen and the sentence the voice reads:
    a 3D headline scene, one slide-in card scene per 1-2 key points, and a closing scene."""
    points = [p.strip() for p in post.points_te if p.strip()]
    if not points:
        raise ReelUnavailable("a reel needs at least one key point")
    head = post.headline_te.strip()
    scenes: list[dict] = [{"style": "three", "head": head, "size": headline_size(head),
                           "count": min(max(len(points), 2), 4),
                           "sub": TOPIC_LABEL.get(post.topic, TOPIC_LABEL["other"]), "say": head,
                           "min": MIN_THREE_SECONDS}]
    start = 0
    for chunk in _chunk_points(points):
        scenes.append({"style": "steps", "items": list(chunk), "start": start,
                       "say": ". ".join(chunk), "min": MIN_STEPS_SECONDS})
        start += len(chunk)
    credit = (post.source_credit or "").strip()
    say = CTA + "."
    sub = brand
    if require_credit and credit:
        say = f"మూలం: {credit}. {CTA}."
        sub = f"మూలం: {credit}"
    scenes.append({"style": "title", "show": brand, "size": 96, "top": 700, "sub": sub, "say": say,
                   "min": MIN_TITLE_SECONDS})
    return scenes


def meta_for(post: RewrittenPost, brand: str) -> dict:
    theme = PALETTE.get(post.topic, DEFAULT_THEME)
    return {"label": brand, "chip": TOPIC_LABEL.get(post.topic, TOPIC_LABEL["other"]),
            "foot": "AI voice",
            "theme": {"top": _hex(theme.bg_top), "bottom": _hex(theme.bg_bottom),
                      "accent": _hex(theme.accent)}}


# -- Chrome over the DevTools protocol ---------------------------------------------------------
def find_chrome() -> str:
    override = os.environ.get("CHROME")
    if override and Path(override).is_file():
        return override
    for name in _CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    if Path(_MAC_CHROME).is_file():
        return _MAC_CHROME
    raise ReelUnavailable("Chrome is not installed")


class AnimBrowser:
    """One headless Chrome showing anim.html. Use as a context manager."""

    def __init__(self, chrome: str | None = None):
        self._chrome_path = chrome
        self._proc = self._ws = None
        self._tmp: Path | None = None
        self._id = 0

    def __enter__(self) -> "AnimBrowser":
        try:
            import websocket
        except ImportError:
            raise ReelUnavailable("websocket-client is not installed") from None
        chrome = self._chrome_path or find_chrome()
        self._tmp = Path(tempfile.mkdtemp(prefix="tgnews-anim-"))
        for src in (ASSETS / "anim.html", ASSETS / "three.min.js", FONT_PATH):
            shutil.copy(src, self._tmp / src.name)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        args = [chrome, "--headless=new", f"--remote-debugging-port={port}",
                f"--user-data-dir={self._tmp / 'profile'}", "--no-first-run", "--hide-scrollbars",
                "--enable-unsafe-swiftshader", "--window-size=1080,1920",
                "--force-device-scale-factor=1", "about:blank"]
        if os.environ.get("CI"):  # GitHub runners: no sandbox user namespaces, small /dev/shm
            args[1:1] = ["--no-sandbox", "--disable-dev-shm-usage"]
        try:
            self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            page = None
            for _ in range(80):
                try:
                    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2))
                    page = next(t for t in tabs if t.get("type") == "page")
                    break
                except Exception:
                    time.sleep(0.25)
            if page is None:
                raise ReelUnavailable("Chrome did not start")
            self._ws = websocket.create_connection(page["webSocketDebuggerUrl"], max_size=None)
            self._cdp("Emulation.setDeviceMetricsOverride", width=1080, height=1920,
                      deviceScaleFactor=1, mobile=False)
            self._cdp("Page.navigate", url=(self._tmp / "anim.html").as_uri())
            for _ in range(120):
                if self.evaluate("document.readyState") == "complete":
                    break
                time.sleep(0.25)
            else:
                raise ReelUnavailable("the animation page did not load")
            if self.evaluate("typeof THREE") == "undefined":
                raise ReelUnavailable("3D library did not load")
        except ReelUnavailable:
            self.__exit__()
            raise
        except Exception as exc:
            self.__exit__()
            raise ReelUnavailable(f"Chrome could not be driven ({type(exc).__name__})") from None
        return self

    def __exit__(self, *exc) -> None:
        for closer in (lambda: self._ws and self._ws.close(),
                       lambda: self._proc and self._proc.terminate(),
                       lambda: self._proc and self._proc.wait(timeout=10)):
            try:
                closer()
            except Exception:
                pass
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)

    def _cdp(self, method: str, **params):
        self._id += 1
        self._ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise ReelUnavailable(f"Chrome rejected {method}")
                return msg["result"]

    def evaluate(self, js: str):
        res = self._cdp("Runtime.evaluate", expression=js, returnByValue=True, awaitPromise=True)
        return res["result"].get("value")

    def setup(self, spec: dict, meta: dict) -> None:
        self.evaluate(f"setup({json.dumps(spec, ensure_ascii=False)}, "
                      f"{json.dumps(meta, ensure_ascii=False)})")

    def frame(self, t: float, duration: float) -> bytes:
        self.evaluate(f"draw({t}, {duration})")
        data = self._cdp("Page.captureScreenshot", format="jpeg", quality=JPEG_QUALITY)["data"]
        return base64.b64decode(data)


# -- encoding -----------------------------------------------------------------------------------
def encode_segment(ffmpeg: str, mp3: Path, out: Path, frames: Iterable[bytes],
                   pad: float = TAIL_SECONDS) -> None:
    cmd = [ffmpeg, "-y", "-v", "error", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg",
           "-i", "-", "-i", str(mp3), "-af", f"apad=pad_dur={pad:.2f}", "-c:v", "libx264",
           "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac",
           "-b:a", "96k", "-ar", "44100", "-shortest", str(out)]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for jpeg in frames:
            proc.stdin.write(jpeg)
        proc.stdin.close()
        code = proc.wait(timeout=300)
    except (OSError, subprocess.SubprocessError):
        raise ReelUnavailable("ffmpeg could not encode the frames") from None
    if code != 0:
        raise ReelUnavailable(f"ffmpeg failed (exit {code})")


# -- the reel -------------------------------------------------------------------------------------
def render_animated(post: RewrittenPost, out_dir, brand: str, *, voice: str, rate: str,
                    require_credit: bool = True,
                    tts: Callable[[str, str, str, Path], None] = edge_tts_voice,
                    run: Callable[[list[str]], str] = _run,
                    browser_factory: Callable[[], AnimBrowser] = AnimBrowser,
                    encoder: Callable[..., None] = encode_segment
                    ) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ffmpeg, ffprobe = _tool("ffmpeg"), _tool("ffprobe")
    scenes, meta, slug = scenes_for(post, brand, require_credit), meta_for(post, brand), _slug(post)
    segments: list[Path] = []
    total = 0.0
    with browser_factory() as browser:
        for i, scene in enumerate(scenes, start=1):
            mp3 = out / f"voice-{slug}-{i}.mp3"
            tts(scene["say"], voice, rate, mp3)
            try:
                seconds = float(run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                     "-of", "csv=p=0", str(mp3)]).strip())
            except ValueError:
                raise ReelUnavailable("could not read the voice length") from None
            length = max(seconds + TAIL_SECONDS, float(scene.get("min", 0)))
            total += length
            if total > MAX_REEL_SECONDS:
                raise ReelUnavailable(f"reel would be longer than {MAX_REEL_SECONDS:.0f} seconds")
            browser.setup(scene, meta)
            n = math.ceil(length * FPS)
            seg = out / f"seg-{slug}-{i}.mp4"
            encoder(ffmpeg, mp3, seg, (browser.frame(f / FPS, length) for f in range(n)),
                    pad=length - seconds)
            segments.append(seg)
    listing = out / f"segments-{slug}.txt"
    listing.write_text("".join(f"file '{s.resolve()}'\n" for s in segments), encoding="utf-8")
    final = out / f"reel-{slug}.mp4"
    run([ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c", "copy", "-movflags", "+faststart", str(final)])
    return [final]
