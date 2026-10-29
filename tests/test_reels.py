"""Reels: narration, video assembly (fake tools), config, Reels publish, video upload, Telegram preview,
and the pipeline path with its fallback to images. No network, no ffmpeg, no real voice."""
import base64
import json
import math
import random
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from PIL import Image
from sqlalchemy import create_engine

from test_pipeline import (NOW, FakeIG, FakeTG, all_posts, engine, make_deps,  # noqa: F401
                           settings, story)
from tgnews import db, gate, pipeline, publish, reel
from tgnews.config import ConfigError, Settings
from tgnews.models import PostState, Story
from tgnews.reel import ReelUnavailable
from tgnews.rewrite import RewrittenPost
from tgnews.storage import StorageError, build_video_uploader
from tgnews.storage_github import GithubImageStore

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32


def post(points=("మొదటి అంశం", "రెండో అంశం", "మూడో అంశం"), credit="The Hindu"):
    return RewrittenPost(headline_te="హైదరాబాద్‌లో భారీ వర్షం", points_te=list(points),
                         caption_te="క్యాప్షన్", hashtags=["#x"], source_credit=credit,
                         topic="weather", headline_style="statement")


# -- config ---------------------------------------------------------------------------------
def test_reel_settings_default_off():
    s = Settings.from_env({})
    assert s.reels_enabled is False
    assert (s.reel_share, s.reel_daily_cap, s.reel_voice, s.reel_rate) == (
        0.2, 5, "te-IN-MohanNeural", "+20%")


def test_reel_settings_parse_and_reject():
    s = Settings.from_env({"REELS_ENABLED": "true", "REEL_SHARE": "0.5", "REEL_DAILY_CAP": "0",
                           "REEL_VOICE": "te-IN-ShrutiNeural", "REEL_RATE": "-10%"})
    assert (s.reels_enabled, s.reel_share, s.reel_daily_cap, s.reel_voice, s.reel_rate) == (
        True, 0.5, 0, "te-IN-ShrutiNeural", "-10%")
    for bad in ({"REELS_ENABLED": "maybe"}, {"REEL_SHARE": "2"}, {"REEL_SHARE": "x"},
                {"REEL_DAILY_CAP": "-1"}, {"REEL_VOICE": "not a voice"}, {"REEL_RATE": "fast"}):
        with pytest.raises(ConfigError):
            Settings.from_env(bad)


# -- narration and frames ---------------------------------------------------------------------
def test_narration_matches_slides_and_reads_credit():
    texts = reel.narration(post(), require_credit=True)
    assert texts[0] == "హైదరాబాద్‌లో భారీ వర్షం"
    assert texts[1] == "మొదటి అంశం. రెండో అంశం"            # 2 points per slide, like the carousel
    assert texts[2] == "మూడో అంశం. మూలం: The Hindu"          # credit read at the end
    assert reel.narration(post(), require_credit=False)[-1] == "మూడో అంశం"


def test_narration_needs_points():
    with pytest.raises(ReelUnavailable):
        reel.narration(post(points=[" "]))


def test_vertical_frame_is_1080x1920_jpeg(tmp_path):
    src = tmp_path / "slide.jpg"
    Image.new("RGB", (1080, 1350), (10, 80, 120)).save(src, "JPEG")
    out = reel.vertical_frame(src, tmp_path / "f" / "frame.jpg")
    with Image.open(out) as im:
        assert im.size == (1080, 1920) and im.format == "JPEG"


# -- render_reel with fake tools -----------------------------------------------------------------
class Tools:
    """Fake ffprobe/ffmpeg/voice that record what they were asked to do."""

    def __init__(self, seconds="3.5"):
        self.seconds, self.cmds, self.spoken = seconds, [], []

    def run(self, cmd):
        self.cmds.append(cmd)
        if "ffprobe" in cmd[0]:
            return self.seconds + "\n"
        Path(cmd[-1]).write_bytes(MP4)
        return ""

    def tts(self, text, voice, rate, path):
        self.spoken.append((text, voice, rate))
        Path(path).write_bytes(b"mp3")


def fake_slides(n):
    def render(p, out_dir, brand):
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(n):
            f = out_dir / f"s{i}.jpg"
            Image.new("RGB", (1080, 1350), (i * 40, 90, 140)).save(f, "JPEG")
            paths.append(f)
        return paths
    return render


@pytest.fixture
def tools(monkeypatch):
    from tgnews import reel_anim
    for mod in (reel, reel_anim):  # reel_anim imported its own reference to _tool
        monkeypatch.setattr(mod, "_tool", lambda name: f"/usr/bin/{name}")
    return Tools()


def test_render_reel_builds_segments_and_joins_them(tmp_path, tools):
    out = reel.render_reel(post(), tmp_path, "Brand", voice="te-IN-MohanNeural", rate="+15%",
                           style="slides", tts=tools.tts, run=tools.run, render_slides=fake_slides(3))
    assert len(out) == 1 and out[0].suffix == ".mp4" and out[0].is_file()
    assert [s[0] for s in tools.spoken][0] == "హైదరాబాద్‌లో భారీ వర్షం"
    assert all(s[1:] == ("te-IN-MohanNeural", "+15%") for s in tools.spoken)
    ffmpeg = [c for c in tools.cmds if "ffmpeg" in c[0]]
    assert len(ffmpeg) == 4  # 3 slide segments + the join
    seg = ffmpeg[0]
    assert "zoompan" in seg[seg.index("-filter_complex") + 1]
    assert seg[seg.index("-t") + 1] == "3.90"  # 3.5 s of voice + 0.4 s tail
    assert "libx264" in seg and "aac" in seg
    join = ffmpeg[-1]
    assert join[join.index("-f") + 1] == "concat" and "+faststart" in join
    assert out[0].name == join[-1].split("/")[-1]


def test_render_reel_too_long_is_unavailable(tmp_path, tools):
    tools.seconds = "40"
    with pytest.raises(ReelUnavailable, match="longer than"):
        reel.render_reel(post(), tmp_path, "B", voice="v-V-x", rate="+0%", style="slides",
                         tts=tools.tts, run=tools.run, render_slides=fake_slides(3))


def test_render_reel_voice_failure_is_unavailable(tmp_path, tools):
    def boom(*a):
        raise ReelUnavailable("voice generation failed (ConnectionError)")
    with pytest.raises(ReelUnavailable, match="voice generation failed"):
        reel.render_reel(post(), tmp_path, "B", voice="v-V-x", rate="+0%", style="slides",
                         tts=boom, run=tools.run, render_slides=fake_slides(3))


def test_missing_ffmpeg_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("FFMPEG", raising=False)
    monkeypatch.setattr(reel.shutil, "which", lambda name: None)
    with pytest.raises(ReelUnavailable, match="ffmpeg is not installed"):
        reel.render_reel(post(), tmp_path, "B", voice="v-V-x", rate="+0%", style="slides")


# -- Instagram Reels container ---------------------------------------------------------------------
class IG:
    ig_user_id = "IG1"

    def __init__(self):
        self.calls = []

    def post(self, path, **params):
        self.calls.append(("POST", path, params))
        return {"id": "C1"} if path.endswith("/media") else {"id": "M1"}

    def get(self, path, **params):
        self.calls.append(("GET", path, params))
        return {"status_code": "FINISHED"}


@pytest.fixture
def pub_engine():
    e = create_engine("sqlite:///:memory:")
    db.init(e)
    db.save_story(e, Story(id="s1", title="t", summary="s", url="https://e.com/a", source_name="x",
                           published_at=NOW, topic_hint=None))
    return e


def test_reel_publishes_as_REELS_container(pub_engine):
    pid = db.save_post(pub_engine, story_id="s1", format="reel", state=PostState.approved)
    ig = IG()
    media = publish.publish_post(pid, ["https://cdn.example.com/v.mp4"], "cap", ig, pub_engine,
                                 sleep=lambda s: None, kind="reel")
    assert media == "M1"
    create = ig.calls[0]
    assert create[1] == "IG1/media"
    assert create[2] == {"media_type": "REELS", "video_url": "https://cdn.example.com/v.mp4",
                         "caption": "cap", "share_to_feed": "true"}
    assert [c[1] for c in ig.calls if c[1].endswith("/media_publish")] == ["IG1/media_publish"]
    assert db.get_post(pub_engine, pid).state == PostState.published


def test_reel_needs_exactly_one_https_url(pub_engine):
    pid = db.save_post(pub_engine, story_id="s1", format="reel", state=PostState.approved)
    for urls in (["https://a/1.mp4", "https://a/2.mp4"], ["http://a/1.mp4"], []):
        with pytest.raises(ValueError):
            publish.publish_post(pid, urls, "c", IG(), pub_engine, kind="reel")
    assert db.get_post(pub_engine, pid).state == PostState.approved  # nothing was claimed


# -- video upload -----------------------------------------------------------------------------------
class Gh:
    def __init__(self, status=201):
        self.status, self.requests = status, []

    def handler(self, request):
        self.requests.append(request)
        return httpx.Response(self.status, json={"message": "x"})


def store(gh, **kw):
    return GithubImageStore("tok", "own/imgs", "main", http=httpx.Client(
        transport=httpx.MockTransport(gh.handler)), sleep=lambda s: None,
        clock=lambda: datetime(2026, 10, 29, 9, 0, tzinfo=timezone.utc), unique=lambda: "abc123", **kw)


def test_upload_video_commits_mp4_and_returns_cdn_url(tmp_path):
    f = tmp_path / "reel.mp4"
    f.write_bytes(MP4)
    gh = Gh()
    url = store(gh).upload_video(f, "2026-10-29/story1")
    assert url == "https://cdn.jsdelivr.net/gh/own/imgs@main/posts/20261029/2026-10-29-story1-abc123.mp4"
    [req] = gh.requests
    body = json.loads(req.content)
    assert req.method == "PUT" and req.url.path.endswith("-abc123.mp4")
    assert base64.b64decode(body["content"]) == MP4 and body["branch"] == "main"


def test_upload_video_rejects_bad_files(tmp_path):
    s = store(Gh())
    notvideo = tmp_path / "a.mp4"
    notvideo.write_bytes(b"not an mp4 at all")
    wrongext = tmp_path / "a.mov"
    wrongext.write_bytes(MP4)
    big = tmp_path / "big.mp4"
    big.write_bytes(MP4 + b"\x00" * (25 * 1024 * 1024))
    for f in (notvideo, wrongext, big, tmp_path / "missing.mp4"):
        with pytest.raises(StorageError):
            s.upload_video(f, "p")


def test_upload_video_never_overwrites(tmp_path):
    f = tmp_path / "r.mp4"
    f.write_bytes(MP4)
    with pytest.raises(StorageError, match="not overwriting"):
        store(Gh(status=422)).upload_video(f, "p")


def test_video_base_override_and_s3_refused():
    assert store(Gh(), video_base="https://v.example.com/x").video_base == "https://v.example.com/x"
    with pytest.raises(StorageError):
        store(Gh(), video_base="http://insecure.example.com")
    with pytest.raises(StorageError, match="github"):
        build_video_uploader({"STORAGE_PROVIDER": "s3"})


# -- Telegram preview -----------------------------------------------------------------------------------
class TG:
    chat_id = "1"

    def __init__(self):
        self.calls, self.n = [], 0

    def call(self, method, **payload):
        self.calls.append((method, payload))
        self.n += 1
        return {"message_id": self.n}


def test_reel_draft_is_previewed_as_a_video(pub_engine):
    pid = db.save_post(pub_engine, story_id="s1", format="reel", state=PostState.draft)
    tg = TG()
    gate.send_draft(pub_engine, tg, pid, ["https://cdn.example.com/v.mp4"], "cap")
    assert tg.calls[0][0] == "sendVideo" and tg.calls[0][1]["video"] == "https://cdn.example.com/v.mp4"
    assert [m for m, _ in tg.calls].count("sendPhoto") == 0
    assert tg.calls[1][0] == "sendMessage"  # the Approve / Reject / Edit message


# -- pipeline -----------------------------------------------------------------------------------------------
def reel_settings(**kw):
    kw.setdefault("reels_enabled", True)
    kw.setdefault("reel_share", 1.0)
    return settings(**kw)


def reel_deps(engine, tmp_path, stories, *, fail=None, **kw):
    made, uploaded = [], []

    def render_reel(p, out_dir, brand):
        if fail:
            raise fail
        out_dir.mkdir(parents=True, exist_ok=True)
        f = out_dir / "reel.mp4"
        f.write_bytes(MP4)
        made.append(f)
        return [f]

    def upload_video(path, prefix):
        uploaded.append(prefix)
        return f"https://cdn.example.com/{prefix}/reel.mp4"

    d = make_deps(engine, tmp_path, stories, tg=kw.pop("tg", FakeTG()), render_reel=render_reel,
                  upload_video=upload_video, **kw)
    d.made, d.uploaded = made, uploaded
    return d


def test_reels_off_by_default_changes_nothing(engine, tmp_path):
    deps = reel_deps(engine, tmp_path, [story(1)])
    run = pipeline.run_once(settings(), deps)  # reels_enabled is False
    assert run.drafted == 1 and deps.made == []
    assert all_posts(engine)[0].format in ("card", "carousel")


def test_reel_becomes_a_draft_with_video_preview(engine, tmp_path):
    tg = FakeTG()
    deps = reel_deps(engine, tmp_path, [story(1)], tg=tg)
    run = pipeline.run_once(reel_settings(), deps)
    assert run.drafted == 1 and len(deps.made) == 1
    [p] = all_posts(engine)
    assert p.format == "reel" and p.state == PostState.draft
    assert tg.calls[0][0] == "sendVideo"
    [row] = engine.connect().execute(db.drafts.select()).all()
    assert row.slides == [f"https://cdn.example.com/2026-10-28/{p.story_id}/reel.mp4"]


def test_reel_failure_falls_back_to_carousel(engine, tmp_path):
    tg = FakeTG()
    deps = reel_deps(engine, tmp_path, [story(1)], tg=tg, fail=ReelUnavailable("voice down"))
    run = pipeline.run_once(reel_settings(), deps)
    assert run.drafted == 1 and run.failed == 0
    [p] = all_posts(engine)
    assert p.format in ("card", "carousel")
    assert tg.calls[0][0] in ("sendPhoto", "sendMediaGroup")


def test_reels_never_auto_publish_even_in_auto_mode(engine, tmp_path):
    ig = FakeIG()
    deps = reel_deps(engine, tmp_path, [story(1)], ig=ig)
    run = pipeline.run_once(reel_settings(mode="auto"), deps)
    assert run.published == 0 and run.drafted == 1
    assert ig.calls == []
    assert all_posts(engine)[0].state == PostState.draft


def test_reel_daily_cap(engine, tmp_path):
    deps = reel_deps(engine, tmp_path, [story(1), story(2), story(3)])
    run = pipeline.run_once(reel_settings(reel_daily_cap=1, per_run_cap=5), deps)
    formats = sorted(p.format for p in all_posts(engine))
    assert run.drafted == 3 and formats.count("reel") == 1


def test_approved_reel_publishes_as_reel(engine, tmp_path):
    ig, tg = FakeIG(), FakeTG()
    deps = reel_deps(engine, tmp_path, [story(1)], ig=ig, tg=tg)
    pipeline.run_once(reel_settings(), deps)
    [p] = all_posts(engine)
    assert db.transition_post_state(engine, p.id, PostState.draft, PostState.approved)
    summary = pipeline.publish_approved(reel_settings(), deps)
    assert summary.published == 1
    assert ig.created[0]["media_type"] == "REELS"
    assert ig.created[0]["video_url"].endswith("/reel.mp4")


def test_poster_edit_is_refused_for_reels(engine, tmp_path):
    deps = reel_deps(engine, tmp_path, [story(1)])
    redraw = pipeline._redrawer(reel_settings(), deps, NOW)
    with pytest.raises(ReelUnavailable):
        redraw(1, post(), "reel")


# -- animated style ---------------------------------------------------------------------------------
from tgnews import reel_anim  # noqa: E402


def test_reel_style_setting():
    assert Settings.from_env({}).reel_style == "animated"
    assert Settings.from_env({"REEL_STYLE": "SLIDES"}).reel_style == "slides"
    with pytest.raises(ConfigError):
        Settings.from_env({"REEL_STYLE": "fancy"})


def test_one_scene_per_sentence_headline_points_then_closing():
    scenes = reel_anim.scenes_for(post(), "Telangana News", require_credit=True)
    assert [s["kind"] for s in scenes] == ["headline", "point", "point", "point", "outro"]
    assert [s["say"] for s in scenes[:4]] == ["హైదరాబాద్‌లో భారీ వర్షం", "మొదటి అంశం", "రెండో అంశం", "మూడో అంశం"]
    assert [s["num"] for s in scenes] == [0, 1, 2, 3, 0]
    assert "ఫాలో" in scenes[-1]["say"] and "మూలం" not in scenes[-1]["say"]  # the reel never reads the source
    assert scenes[0]["first"] and not scenes[1]["first"] and scenes[2]["total"] == 5
    assert all(s["size"] >= 56 for s in scenes)


def test_reel_never_shows_or_reads_the_source_even_when_credit_is_required():
    for need in (True, False):
        scenes = reel_anim.scenes_for(post(), "Brand", require_credit=need)
        assert all("మూలం" not in s["say"] and "The Hindu" not in s["say"] for s in scenes)
        m = reel_anim.meta_for(post(), "Brand", require_credit=need)
        assert "The Hindu" not in json.dumps(m, ensure_ascii=False) and m["ticker"] == "Brand"


def test_effect_follows_the_news_words_then_the_topic():
    def fx(head, topic, points=("అంశం",)):
        return reel_anim.effect_for(replace(post(points=points), headline_te=head, topic=topic))
    assert fx("హైదరాబాద్‌లో భారీ వర్షం", "weather") == "rain"
    assert fx("పంటలను ముంచిన వరద", "farming") == "rain"          # a farming story about a flood
    assert fx("రాష్ట్రంలో ఎండలు మండుతున్నాయి", "weather") == "heat"
    assert fx("ఉదయం పొగమంచు", "weather") == "fog"
    assert fx("వాతావరణ సూచన", "weather", points=("రేపు వర్షం పడొచ్చు",)) == "rain"  # from the points
    assert fx("దొంగతనం కేసు", "crime") == "crime"
    assert fx("రైతులకు సాయం", "farming") == "farming"
    assert fx("ఎన్నికల ప్రచారం", "politics") == "politics"
    assert fx("ఏదో వార్త", "nonsense") == "other"


def test_meta_uses_topic_theme_ticker_and_map():
    m = reel_anim.meta_for(post(), "Brand")
    assert m["theme"]["accent"] == "#64c8ff" and m["chip"] == "వాతావరణం" and m["label"] == "Brand"
    assert m["tag"] == "తాజా వార్త" and m["effect"] == "rain"
    assert m["map"]["d"].startswith("M") and m["map"]["d"].endswith("Z") and m["map"]["h"] > 100
    assert 0 < m["map"]["hx"] < 1000


def test_word_times_come_from_the_voice_or_are_spread_evenly():
    spoken = [(0.09, 0.7, "హైదరాబాద్"), (0.84, 0.3, "భారీ")]
    assert reel_anim.timed_words(spoken, "x", 2.0) == [
        {"t": 0.09, "d": 0.7, "w": "హైదరాబాద్"}, {"t": 0.84, "d": 0.3, "w": "భారీ"}]
    even = reel_anim.timed_words(None, "ఒకటి రెండు మూడు", 3.3)
    assert [w["w"] for w in even] == ["ఒకటి", "రెండు", "మూడు"]
    assert even[0]["t"] == 0.1 and even[0]["t"] < even[1]["t"] < even[2]["t"] < 3.3


class FakeBrowser:
    def __init__(self):
        self.specs, self.frames = [], 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def setup(self, spec, meta):
        self.specs.append(spec)

    def frame(self, t, duration):
        self.frames += 1
        return b"jpeg"


def test_render_animated_times_each_scene_to_its_own_voice(tmp_path, tools):
    browser, encoded = FakeBrowser(), []

    def voice(text, v, r, path):
        Path(path).write_bytes(b"mp3")
        return [(0.1, 0.5, text.split()[0])]  # the voice service's word times

    def encoder(ffmpeg, mp3, seg, frames, pad):
        encoded.append((sum(1 for _ in frames), round(pad, 2)))
        Path(seg).write_bytes(MP4)

    out = reel_anim.render_animated(post(), tmp_path, "Brand", voice="te-IN-MohanNeural", rate="+20%",
                                    tts=voice, run=tools.run, browser_factory=lambda: browser,
                                    encoder=encoder)
    assert out[0].is_file() and len(browser.specs) == 5
    assert browser.specs[0]["words"] == [{"t": 0.1, "d": 0.5, "w": "హైదరాబాద్‌లో"}]
    assert browser.specs[1]["words"][0]["w"] == "మొదటి"
    # 3.5 s of voice + the 0.2 s beat per scene, no idle holding: scene length follows the sentence
    assert encoded == [(math.ceil(3.7 * 24), 0.2)] * 5
    assert [c for c in tools.cmds if "ffmpeg" in c[0]][-1][-1].endswith(".mp4")  # the join


def test_animated_failure_falls_back_to_slideshow(tmp_path, tools):
    def broken(*a, **k):
        raise ReelUnavailable("Chrome is not installed")
    out = reel.render_reel(post(), tmp_path, "B", voice="v-V-x", rate="+0%", tts=tools.tts,
                           run=tools.run, render_slides=fake_slides(3), animated=broken)
    assert out[0].is_file() and any("zoompan" in " ".join(c) for c in tools.cmds)


def test_animated_is_the_default_style(tmp_path, tools):
    called = []

    def fake(*a, **k):
        called.append(1)
        return [tmp_path / "x.mp4"]
    reel.render_reel(post(), tmp_path, "B", voice="v-V-x", rate="+0%", animated=fake)
    assert called == [1]


def test_missing_chrome_is_unavailable(monkeypatch):
    monkeypatch.delenv("CHROME", raising=False)
    monkeypatch.setattr(reel_anim.shutil, "which", lambda n: None)
    monkeypatch.setattr(reel_anim, "_MAC_CHROME", "/no/such/chrome")
    with pytest.raises(ReelUnavailable, match="Chrome is not installed"):
        reel_anim.find_chrome()


# -- voice retries ------------------------------------------------------------------------------------------
def test_voice_is_retried_when_the_free_service_answers_no_audio(tmp_path, monkeypatch):
    import sys
    import types

    class NoAudioReceived(Exception):
        pass

    calls = []

    class Communicate:
        def __init__(self, text, voice, rate=None, boundary=None):
            calls.append(1)

        async def stream(self):
            if len(calls) < 3:
                raise NoAudioReceived("no audio")
            yield {"type": "audio", "data": b"mp3data"}
            yield {"type": "WordBoundary", "offset": 1_000_000, "duration": 5_000_000, "text": "హలో"}

    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(Communicate=Communicate))
    sleeps = []
    words = reel.edge_tts_voice("హలో", "te-IN-MohanNeural", "+20%", tmp_path / "a.mp3", sleep=sleeps.append)
    assert words == [(0.1, 0.5, "హలో")] and len(calls) == 3 and sleeps == [3.0, 6.0]
    assert (tmp_path / "a.mp3").read_bytes() == b"mp3data"


def test_voice_gives_up_after_three_tries_with_a_clear_message(tmp_path, monkeypatch):
    import sys
    import types

    class Communicate:
        def __init__(self, *a, **k):
            pass

        async def stream(self):
            raise RuntimeError("nope")
            yield

    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(Communicate=Communicate))
    with pytest.raises(ReelUnavailable, match=r"RuntimeError\) after 3 tries"):
        reel.edge_tts_voice("x", "v-V-x", "+0%", tmp_path / "a.mp3", sleep=lambda s: None)
