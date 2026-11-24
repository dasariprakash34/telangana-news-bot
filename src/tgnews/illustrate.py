"""Illustrations for the story reels, drawn by Cloudflare Workers AI (FLUX schnell, free daily allowance).

Needs CF_ACCOUNT_ID and CF_API_TOKEN (a token limited to Workers AI). Neither is ever logged. The style
is fixed here so every scene of every story looks like one storybook: flat vector folk illustration,
warm colours, no lettering (all text is drawn by the reel itself, in Telugu, so it is always correct).
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Mapping

import httpx

log = logging.getLogger(__name__)

MODEL = "@cf/black-forest-labs/flux-1-schnell"
STEPS = 8  # the model's maximum: cleaner shapes, still well inside the free allowance
STYLE = ("flat vector illustration in an Indian folk storybook style, like a modern picture book: simple "
         "clean shapes, soft gradients, warm saturated colours (saffron, turmeric yellow, leaf green, "
         "terracotta, deep blue), gentle sunlight, rural Telangana setting, characters in traditional "
         "Telangana clothes with friendly faces, vertical composition with the subject in the centre, "
         "NO text, NO letters, NO words, NO watermark")


class IllustrationUnavailable(RuntimeError):
    """No picture could be drawn. The message never contains the token."""


def configured(env: Mapping[str, str]) -> bool:
    return bool((env.get("CF_ACCOUNT_ID") or "").strip() and (env.get("CF_API_TOKEN") or "").strip())


def draw(scene: str, out: Path, *, seed: int | None = None, env: Mapping[str, str] | None = None,
         http: httpx.Client | None = None) -> Path:
    """Draw one scene (an English description of what is in the picture) into `out` (JPEG)."""
    env = os.environ if env is None else env
    if not configured(env):
        raise IllustrationUnavailable("CF_ACCOUNT_ID / CF_API_TOKEN are not set")
    url = (f"https://api.cloudflare.com/client/v4/accounts/{env['CF_ACCOUNT_ID'].strip()}"
           f"/ai/run/{MODEL}")
    body: dict = {"prompt": f"{scene}. {STYLE}", "steps": STEPS}  # FLUX schnell here takes no seed
    own = http is None
    client = http or httpx.Client(timeout=120)
    try:
        resp = client.post(url, json=body, headers={"Authorization": f"Bearer {env['CF_API_TOKEN'].strip()}"})
    except httpx.HTTPError as exc:
        raise IllustrationUnavailable(f"Workers AI unreachable ({type(exc).__name__})") from None
    finally:
        if own:
            client.close()
    if resp.status_code != 200:
        try:  # Cloudflare's own error text (code + message) never contains the token
            errs = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in resp.json().get("errors", []))[:300]
        except ValueError:
            errs = ""
        raise IllustrationUnavailable(f"Workers AI answered {resp.status_code} {errs}".strip())
    try:
        data = base64.b64decode(resp.json()["result"]["image"])
    except (ValueError, KeyError, TypeError):
        raise IllustrationUnavailable("Workers AI reply had no image") from None
    if not data.startswith((b"\xff\xd8", b"\x89PNG")):
        raise IllustrationUnavailable("Workers AI reply was not a picture")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out


# A fixed test story, to judge the look before the story series is built.
SAMPLE_SCENES = (
    ("Ramappa temple", "The Ramappa temple, an 800-year-old Kakatiya Shiva temple of carved dark stone with a "
     "stepped pyramid tower on a high star-shaped platform, standing in green paddy fields beside a calm lake "
     "at sunrise, palm trees"),
    ("The sculptor", "An old master sculptor named Ramappa in a white dhoti and turban, chiselling a graceful "
     "dancing woman figure on a black stone temple bracket, golden evening light, stone dust in the air"),
    ("Floating bricks", "Villagers in traditional Telangana clothes standing at the edge of a lake, amazed, as "
     "light porous temple bricks float on the water surface; the temple tower in the distance"),
)


def send_sample(env: Mapping[str, str], telegram, out_dir: Path) -> list[str]:
    """Draw the three sample scenes, keep them in `out_dir` and send each to Telegram."""
    report: list[str] = []
    for i, (label, scene) in enumerate(SAMPLE_SCENES, start=1):
        path = Path(out_dir) / f"scene-{i}.jpg"
        try:
            draw(scene, path, seed=100 + i, env=env)
        except IllustrationUnavailable as exc:
            report.append(f"{label}: {exc}")
            continue
        if telegram is not None:
            try:
                telegram.upload("sendPhoto", "photo", path, chat_id=telegram.chat_id,
                                caption=f"Story look test {i}/3: {label}")
            except Exception as exc:  # the files are kept either way
                report.append(f"{label}: drawn, Telegram failed ({type(exc).__name__})")
                continue
        report.append(f"{label}: drawn" + (" and sent" if telegram is not None else ""))
    return report
