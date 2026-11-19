"""A real, freely licensed photo of the story's place for the reel background.

News-agency photos are copyrighted, so the bot never reuses them. It searches Wikimedia Commons for
the place or landmark the story is about (the model gives an English search phrase), keeps only
photos under a free licence (public domain, CC0, CC BY, CC BY-SA), and returns the credit line the
licence asks for, which goes into the caption. Anything that goes wrong returns None and the reel
uses its drawn scene instead: a photo can never stop a story from being posted.
"""
from __future__ import annotations

import html
import json
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "tgnews/1.0 (Telugu news reel bot; python urllib)"
TIMEOUT = 20
MIN_WIDTH = 1000
MAX_BYTES = 8_000_000
NO_PHOTO_TOPICS = ("crime",)  # a real place next to a crime story would point at the wrong people
_FREE = re.compile(r"^(cc0|public domain|pd\b|pd-|cc by(-sa)? \d(\.\d)?)", re.I)
# files that are not a picture of a place: documents, graphics, shops advertising themselves
_BAD_TITLE = re.compile(
    r"map|logo|flag|seal|emblem|locator|diagram|chart|graph|signature|coat of arms|poster|banner|"
    r"screenshot|certificate|document|\bcard\b|service|online|shop|store|office|advert|selfie|"
    r"portrait|\bmla\b|\bmp\b|minister|\bcm\b|election|rally|protest", re.I)


@dataclass(frozen=True)
class Photo:
    path: Path
    credit: str  # "ఫోటో: <author>, <licence>, Wikimedia Commons"
    title: str


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


def _get_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("photo too large")
    return data


def _plain(value: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return " ".join(text.split())


def search_url(query: str) -> str:
    params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": "6",
              "gsrlimit": "15", "gsrsearch": f"{query} filetype:bitmap", "prop": "imageinfo",
              "iiprop": "url|size|mime|extmetadata", "iiurlwidth": "1080",
              "iiextmetadatafilter": "LicenseShortName|Artist"}
    return API + "?" + urllib.parse.urlencode(params)


def pick(pages: dict, query: str) -> dict | None:
    """The most relevant usable result: a free licence, a JPEG/PNG at least MIN_WIDTH wide, and a
    title that names the place rather than a document, graphic or shop. Search order breaks ties."""
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
    best, best_key = None, None
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        title = page.get("title", "")
        licence = _plain(meta.get("LicenseShortName", {}).get("value", ""))
        if not _FREE.match(licence) or _BAD_TITLE.search(title):
            continue
        if info.get("mime") not in ("image/jpeg", "image/png") or info.get("width", 0) < MIN_WIDTH:
            continue
        if not info.get("thumburl"):
            continue
        named = sum(w in title.lower() for w in words)
        if words and named == 0:
            continue
        key = (-named, page.get("index", 99))
        if best_key is None or key < best_key:
            best, best_key = {"title": title, "url": info["thumburl"], "licence": licence,
                              "artist": _plain(meta.get("Artist", {}).get("value", ""))}, key
    return best


def credit_line(artist: str, licence: str) -> str:
    who = artist[:60].rstrip(" ,;") or "Wikimedia Commons contributor"
    return f"ఫోటో: {who}, {licence}, Wikimedia Commons"


def find_photo(query: str | None, topic: str | None, out_dir: Path,
               get_json: Callable[[str], dict] = _get_json,
               get_bytes: Callable[[str], bytes] = _get_bytes) -> Photo | None:
    query = " ".join((query or "").split())[:80]
    if not query or topic in NO_PHOTO_TOPICS:
        return None
    try:
        found = pick(get_json(search_url(query)).get("query", {}).get("pages", {}), query)
        if found is None:
            log.info("no free photo for %r", query)
            return None
        data = get_bytes(found["url"])
        if not data.startswith((b"\xff\xd8", b"\x89PNG")):
            return None
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / ("photo" + (".png" if data.startswith(b"\x89PNG") else ".jpg"))
        path.write_bytes(data)
        return Photo(path=path, credit=credit_line(found["artist"], found["licence"]), title=found["title"])
    except Exception as exc:  # network, JSON, disk: the reel falls back to its drawn scene
        log.warning("photo lookup failed for %r (%s)", query, type(exc).__name__)
        return None


def attach(post, photo: Photo | None) -> bool:
    """Put `photo` on the post: the reel draws it, the caption carries its licence credit, and the
    reel's look is recorded as "photo" so the learning loop can compare it with the drawn scenes."""
    if photo is None:
        return False
    post.photo_path, post.photo_credit, post.design = str(photo.path), photo.credit, "photo"
    if photo.credit not in post.caption_te:
        post.caption_te = f"{post.caption_te.rstrip()}\n\n{photo.credit}"
    return True
