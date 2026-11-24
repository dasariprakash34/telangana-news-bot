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
    r"portrait|\bmla\b|\bmp\b|minister|\bcm\b|election|rally|protest"
    r"|butterfly|\bmoth\b|insect|beetle|\bbird\b|species|specimen|underside|\bmale\b|\bfemale\b|herbarium", re.I)

# Commons search exclusions: place names that are also animal or plant names (Tirumala is a butterfly genus)
_EXCLUDE = "-butterfly -butterflies -insect -insecta -lepidoptera -species -specimen -herbarium"

@dataclass(frozen=True)
class Photo:
    path: Path
    credit: str  # "ఫోటో: <author>, <licence>, Wikimedia Commons" (every author when there are several)
    title: str
    more: tuple[Path, ...] = ()  # further photos of the same place, shown in turn as the reel moves on


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
              "gsrlimit": "15", "gsrsearch": f"{query} filetype:bitmap {_EXCLUDE}", "prop": "imageinfo",
              "iiprop": "url|size|mime|extmetadata", "iiurlwidth": "1080",
              "iiextmetadatafilter": "LicenseShortName|Artist"}
    return API + "?" + urllib.parse.urlencode(params)


MAX_PHOTOS = 3  # a different real photo every sentence or two, like the scene changes in reels that travel


def pick(pages: dict, query: str) -> dict | None:
    """The most relevant usable result (see pick_all)."""
    found = pick_all(pages, query)
    return found[0] if found else None


def pick_all(pages: dict, query: str) -> list[dict]:
    """Usable results, best first: a free licence, a JPEG/PNG at least MIN_WIDTH wide, and a title
    that names the place rather than a document, graphic or shop. Search order breaks ties."""
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
    ranked = []
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
        if named < len(words):  # every word: "Tirumala temple" must not land on a Kerala Tirumala temple
            continue
        ranked.append(((-named, page.get("index", 99)), {
            "title": title, "url": info["thumburl"], "licence": licence,
            "artist": _plain(meta.get("Artist", {}).get("value", ""))}))
    return [r for _k, r in sorted(ranked, key=lambda kr: kr[0])]


def credit_line(artist: str, licence: str) -> str:
    who = artist[:60].rstrip(" ,;") or "Wikimedia Commons contributor"
    return f"ఫోటో: {who}, {licence}, Wikimedia Commons"


def credits_line(found: list[dict]) -> str:
    """One credit naming every photo's author and licence (what CC BY and CC BY-SA ask for)."""
    if len(found) == 1:
        return credit_line(found[0]["artist"], found[0]["licence"])
    parts = list(dict.fromkeys(f"{(f['artist'][:40].rstrip(' ,;') or 'Wikimedia Commons contributor')} "
                               f"({f['licence']})" for f in found))
    return "ఫోటోలు: " + "; ".join(parts) + ", Wikimedia Commons"


def find_photo(query: str | None, topic: str | None, out_dir: Path,
               get_json: Callable[[str], dict] = _get_json,
               get_bytes: Callable[[str], bytes] = _get_bytes) -> Photo | None:
    query = " ".join((query or "").split())[:80]
    if not query or topic in NO_PHOTO_TOPICS:
        return None
    try:
        candidates = pick_all(get_json(search_url(query)).get("query", {}).get("pages", {}), query)
        if not candidates:
            log.info("no free photo for %r", query)
            return None
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        kept, paths = [], []
        for found in candidates:
            if len(kept) == MAX_PHOTOS:
                break
            try:
                data = get_bytes(found["url"])
            except Exception:  # one bad download only costs that photo
                continue
            if not data.startswith((b"\xff\xd8", b"\x89PNG")):
                continue
            path = out / (f"photo-{len(kept) + 1}" + (".png" if data.startswith(b"\x89PNG") else ".jpg"))
            path.write_bytes(data)
            kept.append(found)
            paths.append(path)
        if not kept:
            return None
        return Photo(path=paths[0], credit=credits_line(kept), title=kept[0]["title"], more=tuple(paths[1:]))
    except Exception as exc:  # network, JSON, disk: the reel falls back to its drawn scene
        log.warning("photo lookup failed for %r (%s)", query, type(exc).__name__)
        return None


def attach(post, photo: Photo | None) -> bool:
    """Put `photo` on the post: the reel draws it, the caption carries its licence credit, and the
    reel's look is recorded as "photo" so the learning loop can compare it with the drawn scenes."""
    if photo is None:
        return False
    post.photo_path, post.photo_credit, post.design = str(photo.path), photo.credit, "photo"
    post.photo_more = [str(p) for p in photo.more]
    if photo.credit not in post.caption_te:
        post.caption_te = f"{post.caption_te.rstrip()}\n\n{photo.credit}"
    return True
