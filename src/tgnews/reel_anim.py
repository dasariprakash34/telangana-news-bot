"""Animated news reel: kinetic Telugu captions timed to the voice, over a topic-coloured motion
background with a 3D glass headline card, a drawing Telangana outline and a news ticker.

Each sentence is its own scene with its own voice clip, so the text on screen starts exactly when its
sentence starts and every word lights up as it is spoken (word times come from the voice service).
`assets/reel/anim.html` exposes `setup(spec, meta)` and `draw(t, duration)`; this module stages it
with the Telugu font in a temp directory, drives one headless Chrome over the DevTools protocol, takes
a JPEG per frame and pipes the frames to ffmpeg with the voice. Chrome shapes Telugu itself, so no
Pillow/raqm is needed. Anything that goes wrong raises `ReelUnavailable`; `reel.render_reel` then
falls back to the plain slideshow reel.
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

from .reel import MAX_REEL_SECONDS, ReelUnavailable, _run, _tool, edge_tts_voice
from .render import DEFAULT_THEME, FONT_PATH, PALETTE, TOPIC_LABEL, _map_data, _slug
from .rewrite import RewrittenPost

ASSETS = Path(__file__).resolve().parent / "assets" / "reel"
FPS = 24
JPEG_QUALITY = 85
SCENE_TAIL = 0.2  # a short beat after a sentence, then a hard cut: reels do not idle
CTA = "మరిన్ని తెలంగాణ వార్తల కోసం ఫాలో అవ్వండి"
TICKER_TAG = "తాజా వార్త"
_CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
_MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _hex(rgb) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


# -- scenes -------------------------------------------------------------------------------------
def caption_size(text: str, headline: bool) -> int:
    n = len(text)
    if headline:
        return 104 if n <= 40 else 90 if n <= 80 else 76
    return 92 if n <= 50 else 80 if n <= 90 else 68  # the page shrinks it further to fit the card


def scenes_for(post: RewrittenPost, brand: str, require_credit: bool = True) -> list[dict]:
    """One scene per sentence: the headline (on the 3D card), each key point, then the closing line.
    `say` is exactly what the voice reads; `words` is filled in later with the voice's word times."""
    points = [p.strip() for p in post.points_te if p.strip()]
    if not points:
        raise ReelUnavailable("a reel needs at least one key point")
    head = post.headline_te.strip()
    credit = (post.source_credit or "").strip()
    closing = f"మూలం: {credit}. {CTA}." if require_credit and credit else f"{CTA}."
    sayings = [("headline", head), *[("point", p) for p in points], ("outro", closing)]
    total = len(sayings)
    scenes = []
    for i, (kind, text) in enumerate(sayings):
        scenes.append({"kind": kind, "say": text, "index": i, "total": total, "first": i == 0,
                       "num": i if kind == "point" else 0,
                       "size": caption_size(text, kind == "headline")})
    return scenes


def even_words(text: str, seconds: float) -> list[dict]:
    """Fallback word times (spread over the clip by word length) when the voice gave none."""
    tokens = text.split()
    weights = [max(1, len(t)) for t in tokens]
    total, t = sum(weights) or 1, 0.1
    span = max(0.1, seconds - 0.3)
    out = []
    for tok, w in zip(tokens, weights):
        d = span * w / total
        out.append({"t": round(t, 3), "d": round(d, 3), "w": tok})
        t += d
    return out


def timed_words(words, text: str, seconds: float) -> list[dict]:
    if words:
        return [{"t": round(float(s), 3), "d": round(float(d), 3), "w": str(w)} for s, d, w in words]
    return even_words(text, seconds)


def map_shape() -> dict:
    """The Telangana outline as an SVG path in a 1000-wide box, plus the Hyderabad dot."""
    data = _map_data()
    ring = data["ring"]
    lons, lats = [p[0] for p in ring], [p[1] for p in ring]
    kx = math.cos(math.radians(sum(lats) / len(lats)))
    minx, maxx, miny, maxy = min(lons), max(lons), min(lats), max(lats)
    scale = 1000 / ((maxx - minx) * kx)

    def proj(lon, lat):
        return ((lon - minx) * kx * scale, (maxy - lat) * scale)

    pts = [proj(*p) for p in ring]
    d = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts) + " Z"
    hx, hy = proj(*data["hyderabad"])
    return {"d": d, "w": 1000, "h": round((maxy - miny) * scale), "hx": round(hx, 1), "hy": round(hy, 1)}


def meta_for(post: RewrittenPost, brand: str, require_credit: bool = True) -> dict:
    theme = PALETTE.get(post.topic, DEFAULT_THEME)
    credit = (post.source_credit or "").strip()
    return {"label": brand, "chip": TOPIC_LABEL.get(post.topic, TOPIC_LABEL["other"]),
            "tag": TICKER_TAG, "credit": f"మూలం: {credit}" if require_credit and credit else "",
            "foot": "AI voice", "map": map_shape(),
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
        for src in (ASSETS / "anim.html", FONT_PATH):
            shutil.copy(src, self._tmp / src.name)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        args = [chrome, "--headless=new", f"--remote-debugging-port={port}",
                f"--user-data-dir={self._tmp / 'profile'}", "--no-first-run", "--hide-scrollbars",
                "--window-size=1080,1920", "--force-device-scale-factor=1", "about:blank"]
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
            self.evaluate("document.fonts.ready.then(() => true)")  # wait for the Telugu font
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
def encode_segment(ffmpeg: str, mp3: Path, out: Path, frames: Iterable[bytes], pad: float) -> None:
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
                    tts: Callable = edge_tts_voice,
                    run: Callable[[list[str]], str] = _run,
                    browser_factory: Callable[[], AnimBrowser] = AnimBrowser,
                    encoder: Callable[..., None] = encode_segment) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ffmpeg, ffprobe = _tool("ffmpeg"), _tool("ffprobe")
    scenes, meta, slug = scenes_for(post, brand, require_credit), meta_for(post, brand, require_credit), _slug(post)
    segments: list[Path] = []
    total = 0.0
    with browser_factory() as browser:
        for i, scene in enumerate(scenes, start=1):
            mp3 = out / f"voice-{slug}-{i}.mp3"
            words = tts(scene["say"], voice, rate, mp3)
            try:
                seconds = float(run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                     "-of", "csv=p=0", str(mp3)]).strip())
            except ValueError:
                raise ReelUnavailable("could not read the voice length") from None
            length = seconds + SCENE_TAIL
            total += length
            if total > MAX_REEL_SECONDS:
                raise ReelUnavailable(f"reel would be longer than {MAX_REEL_SECONDS:.0f} seconds")
            scene["words"] = timed_words(words, scene["say"], seconds)
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
