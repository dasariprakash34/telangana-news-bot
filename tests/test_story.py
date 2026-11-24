import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from tgnews import db, illustrate, story
from tgnews.config import Settings
from tgnews.models import PostState
from tgnews.pipeline import Deps

NOW = datetime(2026, 11, 25, 1, 40, tzinfo=timezone.utc)
FACTS = ("The Ramappa Temple was built in 1213 by Recharla Rudra, a general of the Kakatiya king Ganapati Deva. "
         "It is named after its chief sculptor Ramappa. Its bricks are so light that they float on water. ") * 12
LINE = "మన ఊరి పెద్దలు ఈ గుడి గురించి ఎంతో ప్రేమతో ముచ్చట చెప్తారు అంటారు చూడండి"


def scenes(n=12, first="మీకు మన రామప్ప గుడి గురించి ఎరుకేనా? ఈ గుడి 1213 లో కట్టిండ్రు అంటారు"):
    out = [{"say_te": first, "title_te": "", "image_en": "the temple at sunrise"}]
    out += [{"say_te": LINE, "title_te": "కథ" if i == 3 else "", "image_en": f"scene {i}"} for i in range(2, n + 1)]
    return out


def script_json(**over):
    data = {"cover_te": "రామప్ప గుడి ఇటుకలు నీళ్లలో తేలుతయ్!", "hero_en": "a dark stone Kakatiya temple",
            "scenes": scenes(), "caption_te": "రామప్ప గుడి కథ ఇది. మీ ఊర్లో ఈ కథ ఎట్లా చెప్తారు? కామెంట్ చేయండి.",
            "hashtags": ["#Ramappa", "#Telangana", "#Warangal"]}
    data.update(over)
    return json.dumps(data, ensure_ascii=False)


class LLM:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return SimpleNamespace(content=[SimpleNamespace(text=text)], stop_reason="end_turn")


class TG:
    chat_id = "1"

    def __init__(self):
        self.calls, self.n = [], 0

    def call(self, method, **payload):
        self.calls.append((method, payload))
        self.n += 1
        return {"message_id": self.n}


def jpeg(path, size=(1024, 1024)):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (200, 120, 40)).save(path, "JPEG")
    return Path(path)


# -- topics, facts and the script ------------------------------------------------------------------
def test_the_shipped_topic_list_loads_and_is_unique():
    topics = story.load_topics("config/stories.yaml")
    assert len(topics) >= 30 and len({t.slug for t in topics}) == len(topics)
    assert all(t.kind in story.CLOSING for t in topics)


def test_fetch_facts_drops_reference_sections():
    body = {"query": {"pages": {"1": {"extract": "Story text.\n\n== History ==\nMore.\n\n== References ==\nRef list"}}}}
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    assert story.fetch_facts("Ramappa Temple", http) == "Story text.\n\n== History ==\nMore."


def test_a_good_script_passes_and_the_prompt_asks_for_the_closing_of_its_kind():
    llm = LLM(script_json())
    s = story.write_script(llm, "m", story.Topic("ramappa", "Ramappa Temple", "temple"), FACTS)
    assert len(s.scenes) == 12 and s.cover_te.startswith("రామప్ప")
    assert "కి జై!" in llm.calls[0]["system"] and "ఎరుకేనా" in llm.calls[0]["system"]


@pytest.mark.parametrize("bad, expect", [
    (dict(scenes=scenes(first="రామప్ప గుడి చాలా పాతది మన ఊర్ల")), "must ask the viewer a question"),
    (dict(scenes=scenes(n=4)), "need 10 to 14"),
    (dict(scenes=scenes(first="మీకు మన రామప్ప గుడి గురించి ఎరుకేనా? ఈ గుడి 1500 లో కట్టిండ్రు")), "not in the source"),
    (dict(cover_te="రामప్ప गुडि"), "another script"),
])
def test_validation_catches_what_would_mislead_or_lose_viewers(bad, expect):
    problems = story.validate(story._parse(script_json(**bad)), FACTS)
    assert any(expect in p for p in problems), problems


# -- pictures --------------------------------------------------------------------------------------
def test_scene_pictures_cover_is_the_poster_style_edges_cropped_and_one_retry(tmp_path):
    calls, fails = [], {"n": 1}

    def draw(prompt, out, style=None):
        calls.append(style)
        if fails["n"]:
            fails["n"] -= 1
            raise illustrate.IllustrationUnavailable("busy")
        return jpeg(out)

    s = story._parse(script_json())
    paths = story.draw_scenes(s, tmp_path, draw=draw, pause=lambda x: None)
    assert len(paths) == 12 and calls[:2] == ["bold_poster", "bold_poster"] and set(calls[2:]) == {"storybook"}
    with Image.open(paths[0]) as im:
        assert im.size == (story.ART, story.ART)


# -- the daily run --------------------------------------------------------------------------------
def make_deps(tmp_path, llm):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.init(engine)
    deps = Deps(fetch=lambda: [], llm=llm, llm_model="m", engine=engine, telegram=TG(),
                upload_video=lambda p, prefix: f"https://cdn.example.com/{prefix}/reel.mp4",
                now=lambda: NOW, out_dir=tmp_path)
    return deps


def wiki(text=FACTS):
    return httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"query": {"pages": {"1": {"extract": text}}}})))


def fake_render(script, images, out_dir, brand, **kw):
    out = Path(out_dir) / "reel-x.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"mp4")
    return out


def test_run_makes_one_draft_per_topic_and_never_publishes(tmp_path, monkeypatch):
    monkeypatch.setattr(story, "render_story", fake_render)
    deps = make_deps(tmp_path, LLM(script_json()))
    topics = [story.Topic("ramappa", "Ramappa Temple", "temple"), story.Topic("charminar", "Charminar", "place")]
    draw = lambda prompt, out, style=None: jpeg(out)
    out = story.run_story(Settings(), deps, topics=topics, http=wiki(), draw=draw, tts=lambda *a: [])
    assert (out.status, out.topic) == ("created", "ramappa")
    post = db.get_post(deps.engine, out.post_id)
    assert post.format == "reel" and post.state == PostState.draft
    assert "మూలం: Wikipedia (Ramappa Temple)" in post.caption and "#Ramappa" in post.caption
    assert deps.telegram.calls[0][0] == "sendVideo"
    again = story.run_story(Settings(), deps, topics=topics, http=wiki(), draw=draw, tts=lambda *a: [])
    assert again.topic == "charminar"  # a topic is told once


def test_a_short_article_is_skipped_and_a_failed_story_is_released_for_another_day(tmp_path, monkeypatch):
    monkeypatch.setattr(story, "render_story", fake_render)
    deps = make_deps(tmp_path, LLM(script_json(scenes=scenes(n=3))))  # rejected twice
    topics = [story.Topic("ramappa", "Ramappa Temple", "temple")]
    out = story.run_story(Settings(), deps, topics=topics, http=wiki("too short"), draw=lambda *a, **k: None)
    assert out.status == "no_topic" and "too short" in out.errors[0]
    out = story.run_story(Settings(), deps, topics=topics, http=wiki(), draw=lambda p, o, style=None: jpeg(o))
    assert out.status == "failed" and "need 10 to 14" in out.detail
    assert not db.story_seen(deps.engine, "story:ramappa")  # released: tried again on a later day
    assert "not made" in deps.telegram.calls[-1][1]["text"]
