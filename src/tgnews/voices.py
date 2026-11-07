"""Choose the voice for a reel: ElevenLabs while it has credits, the free Microsoft voice otherwise.

ElevenLabs is used only when ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID are set. Before a reel is made its
spoken length is checked against the credits left, so one reel never changes voice half way; if ElevenLabs
still fails on a sentence (out of credits, busy, a language the model does not do), that sentence is spoken by
the free voice and the reel is still made. The API key is never logged.
"""
from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path
from typing import Callable, Mapping

import httpx

from .reel import ReelUnavailable, edge_tts_voice
from .rewrite import RewrittenPost

log = logging.getLogger(__name__)

API = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL = "eleven_v3"
DEFAULT_LANGUAGE = "te"  # Telugu: the model must not guess (it drifted into Hindi)
DEFAULT_STABILITY = 1.0  # v3 "Robust": sticks to the script
EXTRA_CHARS = 60  # the closing "follow" line the reel adds
MIN_SPEED, MAX_SPEED = 0.7, 1.2  # the range ElevenLabs accepts


class ElevenUnavailable(Exception):
    """ElevenLabs could not speak this text. The message is safe to log (no key, no text)."""


def configured(env: Mapping[str, str]) -> bool:
    return bool((env.get("ELEVENLABS_API_KEY") or "").strip() and (env.get("ELEVENLABS_VOICE_ID") or "").strip())


def speed_for(rate: str) -> float:
    """The reel's "+20%" speaking rate as ElevenLabs' speed multiplier (1.2), within what it allows."""
    try:
        pct = int(rate.strip().rstrip("%"))
    except ValueError:
        return 1.0
    return max(MIN_SPEED, min(MAX_SPEED, 1 + pct / 100))


def spoken_chars(post: RewrittenPost) -> int:
    return len(post.headline_te) + sum(len(p) for p in post.points_te) + EXTRA_CHARS


def credits_left(http: httpx.Client, key: str) -> int | None:
    """Characters left this billing period, or None when it cannot be read (the key may lack permission)."""
    try:
        resp = http.get(f"{API}/user/subscription", headers={"xi-api-key": key}, timeout=20)
        if resp.status_code != 200:
            return None
        body = resp.json()
        return int(body["character_limit"]) - int(body["character_count"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


def words_from_alignment(alignment: Mapping | None, text: str) -> list[tuple[float, float, str]]:
    """(start, duration, word) per whitespace-separated word from ElevenLabs' character times."""
    if not alignment:
        return []
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    if not (len(chars) == len(starts) == len(ends)) or not chars:
        return []
    words, cur, begin, last = [], "", 0.0, 0.0
    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if cur:
                words.append((begin, max(0.05, last - begin), cur))
                cur = ""
            continue
        if not cur:
            begin = s
        cur += ch
        last = e
    if cur:
        words.append((begin, max(0.05, last - begin), cur))
    return words


def _reason(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
        status = detail.get("status") if isinstance(detail, dict) else None
    except (ValueError, AttributeError):
        status = None
    return f"HTTP {resp.status_code}" + (f" {status}" if status else "")


def request_bodies(text: str, model: str, rate: str, language: str, stability: float) -> list[dict]:
    """The request, then simpler ones for a model that refuses a setting: language locked + steady voice +
    speed, then without the language lock, then without voice settings, then just the text."""
    full = {"text": text, "model_id": model, "voice_settings": {"speed": speed_for(rate), "stability": stability}}
    if language:
        full["language_code"] = language
    bodies = [full]
    if language:
        bodies.append({k: v for k, v in full.items() if k != "language_code"})
    bodies.append({k: v for k, v in full.items() if k not in ("voice_settings",)})
    bodies.append({"text": text, "model_id": model})
    out: list[dict] = []
    for b in bodies:
        if b not in out:
            out.append(b)
    return out


def elevenlabs_voice(text: str, rate: str, path: Path, *, env: Mapping[str, str],
                     http: httpx.Client) -> list[tuple[float, float, str]]:
    """Speak `text` into the mp3 at `path`; returns word times (empty when the model gives none).

    The language is locked (ELEVENLABS_LANGUAGE, default te) because the multilingual model otherwise guesses
    it per sentence and can drift into Hindi; the voice is set steady (ELEVENLABS_STABILITY, default 1.0)."""
    key, voice = env["ELEVENLABS_API_KEY"].strip(), env["ELEVENLABS_VOICE_ID"].strip()
    model = (env.get("ELEVENLABS_MODEL") or "").strip() or DEFAULT_MODEL
    language = (env.get("ELEVENLABS_LANGUAGE") if "ELEVENLABS_LANGUAGE" in env else DEFAULT_LANGUAGE) or ""
    try:
        stability = float((env.get("ELEVENLABS_STABILITY") or "").strip() or DEFAULT_STABILITY)
    except ValueError:
        stability = DEFAULT_STABILITY
    headers = {"xi-api-key": key}
    params = {"output_format": "mp3_44100_128"}
    # with word times first; a model that cannot do that gets the plain request
    attempts = [(f"/text-to-speech/{voice}/with-timestamps", b) for b in request_bodies(text, model, rate, language.strip(), stability)]
    attempts.append((f"/text-to-speech/{voice}", {"text": text, "model_id": model}))
    for endpoint, payload in attempts:
        try:
            resp = http.post(API + endpoint, headers=headers, params=params, json=payload, timeout=120)
        except httpx.HTTPError as exc:
            raise ElevenUnavailable(f"ElevenLabs unreachable ({type(exc).__name__})") from None
        if resp.status_code == 200:
            break
        if resp.status_code not in (400, 422):
            raise ElevenUnavailable(f"ElevenLabs refused ({_reason(resp)})")
    else:
        raise ElevenUnavailable(f"ElevenLabs refused ({_reason(resp)})")
    if "with-timestamps" in endpoint:
        data = resp.json()
        audio = base64.b64decode(data.get("audio_base64") or "")
        words = words_from_alignment(data.get("alignment") or data.get("normalized_alignment"), text)
    else:
        audio, words = resp.content, []
    if not audio:
        raise ElevenUnavailable("ElevenLabs sent no audio")
    path.write_bytes(audio)
    return words


def pick_tts(post: RewrittenPost, env: Mapping[str, str] | None = None, *,
             http: httpx.Client | None = None) -> Callable:
    """The speaking function for this reel (same shape as edge_tts_voice)."""
    env = os.environ if env is None else env
    if not configured(env):
        return edge_tts_voice
    own = http is None
    client = http or httpx.Client()
    left = credits_left(client, env["ELEVENLABS_API_KEY"].strip())
    need = spoken_chars(post)
    if left is not None and left < need:
        log.info("ElevenLabs has %d credits left and this reel needs about %d: using the free voice", left, need)
        if own:
            client.close()
        return edge_tts_voice
    state = {"down": False}

    def speak(text: str, voice: str, rate: str, path: Path, sleep=None):
        if not state["down"]:
            try:
                return elevenlabs_voice(text, rate, path, env=env, http=client)
            except ElevenUnavailable as exc:
                state["down"] = True  # do not keep asking for the rest of this reel
                log.warning("%s; the rest of this reel uses the free voice", exc)
        return edge_tts_voice(text, voice, rate, path) if sleep is None else edge_tts_voice(text, voice, rate, path, sleep)

    return speak


SAMPLE_TEXT = ("తిరుమలలో శ్రీవారి ఆర్జిత సేవా టికెట్లు ఈరోజు మధ్యాహ్నం 3 గంటలకు అందుబాటులోకి వస్తాయి. "
               "ఖమ్మం జిల్లాలో భారీ వర్షాల కారణంగా పలు రోడ్లు జలమయమయ్యాయి. "
               "ఏఐ టెక్నాలజీతో కొత్త ఫోన్ మార్కెట్లోకి వచ్చింది, ధర 25,000 రూపాయలు.")
SAMPLE_VARIANTS = (
    ("A: Telugu locked, steady (the bot's default)", {"ELEVENLABS_LANGUAGE": "te", "ELEVENLABS_STABILITY": "1.0"}),
    ("B: Telugu locked, natural", {"ELEVENLABS_LANGUAGE": "te", "ELEVENLABS_STABILITY": "0.5"}),
    ("C: no language lock (what drifted)", {"ELEVENLABS_LANGUAGE": "", "ELEVENLABS_STABILITY": "0.5"}),
)


def send_samples(env: Mapping[str, str], telegram, out_dir: Path, *, http: httpx.Client | None = None) -> list[str]:
    """Make the same Telugu lines in each variant and send each to Telegram; returns what was sent or why not."""
    own = http is None
    client = http or httpx.Client()
    report: list[str] = []
    try:
        for label, extra in SAMPLE_VARIANTS:
            path = out_dir / f"sample-{label[0]}.mp3"
            try:
                elevenlabs_voice(SAMPLE_TEXT, "+20%", path, env={**env, **extra}, http=client)
            except ElevenUnavailable as exc:
                report.append(f"{label}: {exc}")
                continue
            telegram.upload("sendAudio", "audio", path, chat_id=telegram.chat_id, caption=f"Voice test {label}",
                            title=label[:30])
            report.append(f"{label}: sent")
    finally:
        if own:
            client.close()
    return report
