import base64
import json

import httpx
import pytest

from tgnews import voices
from tgnews.rewrite import RewrittenPost

ENV = {"ELEVENLABS_API_KEY": "sk_test_key_123", "ELEVENLABS_VOICE_ID": "VOICE1", "ELEVENLABS_MODEL": "eleven_v3"}
POST = RewrittenPost(headline_te="తిరుమలలో టికెట్లు", points_te=["ఒకటి", "రెండు"], caption_te="", hashtags=[],
                     source_credit="", topic="temples", headline_style="statement")
ALIGN = {"characters": list("ab cd"), "character_start_times_seconds": [0, .1, .2, .3, .4],
         "character_end_times_seconds": [.1, .2, .3, .4, .5]}


def client(handler):
    calls = []

    def wrapped(request):
        calls.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        return handler(request)

    c = httpx.Client(transport=httpx.MockTransport(wrapped))
    c.calls = calls
    return c


def ok_timestamps(request):
    if request.url.path.endswith("/user/subscription"):
        return httpx.Response(200, json={"character_limit": 30000, "character_count": 1000})
    return httpx.Response(200, json={"audio_base64": base64.b64encode(b"MP3").decode(), "alignment": ALIGN})


def test_not_configured_uses_the_free_voice():
    assert voices.pick_tts(POST, {}) is voices.edge_tts_voice
    assert voices.pick_tts(POST, {"ELEVENLABS_API_KEY": "x"}) is voices.edge_tts_voice  # no voice id


def test_the_reels_rate_becomes_elevenlabs_speed():
    assert voices.speed_for("+20%") == 1.2 and voices.speed_for("+10%") == 1.1
    assert voices.speed_for("+30%") == 1.2 and voices.speed_for("-50%") == 0.7 and voices.speed_for("x") == 1.0


def test_character_times_become_word_times():
    assert voices.words_from_alignment(ALIGN, "ab cd") == [(0, .2, "ab"), (.3, pytest.approx(.2), "cd")]
    assert voices.words_from_alignment(None, "x") == [] and voices.words_from_alignment({"characters": []}, "x") == []


def test_it_speaks_with_word_times_and_never_sends_the_key_in_the_body(tmp_path):
    c = client(ok_timestamps)
    speak = voices.pick_tts(POST, ENV, http=c)
    words = speak("ab cd", "te-IN-MohanNeural", "+20%", tmp_path / "v.mp3")
    assert (tmp_path / "v.mp3").read_bytes() == b"MP3" and [w[2] for w in words] == ["ab", "cd"]
    method, path, body = c.calls[-1]
    assert path.endswith("/text-to-speech/VOICE1/with-timestamps") and body["model_id"] == "eleven_v3"
    assert body["voice_settings"] == {"speed": 1.2} and "sk_test" not in json.dumps(body)


def test_too_few_credits_for_the_whole_reel_uses_the_free_voice_from_the_start():
    low = client(lambda r: httpx.Response(200, json={"character_limit": 30000, "character_count": 29990}))
    assert voices.pick_tts(POST, ENV, http=low) is voices.edge_tts_voice
    assert all("text-to-speech" not in c[1] for c in low.calls)


def test_credits_that_cannot_be_read_are_not_a_reason_to_stop(tmp_path):
    def handler(request):
        if request.url.path.endswith("/user/subscription"):
            return httpx.Response(401, json={"detail": {"status": "missing_permissions"}})
        return ok_timestamps(request)

    speak = voices.pick_tts(POST, ENV, http=client(handler))
    assert speak is not voices.edge_tts_voice
    assert speak("ab cd", "v", "+20%", tmp_path / "v.mp3")


def test_a_model_without_word_times_gets_the_plain_request(tmp_path):
    def handler(request):
        if request.url.path.endswith("/user/subscription"):
            return httpx.Response(200, json={"character_limit": 30000, "character_count": 0})
        if request.url.path.endswith("with-timestamps"):
            return httpx.Response(400, json={"detail": {"status": "unsupported"}})
        return httpx.Response(200, content=b"PLAINMP3")

    c = client(handler)
    words = voices.pick_tts(POST, ENV, http=c)("ab cd", "v", "+20%", tmp_path / "v.mp3")
    assert words == [] and (tmp_path / "v.mp3").read_bytes() == b"PLAINMP3"  # the reel spreads the words itself
    assert "voice_settings" not in c.calls[-1][2]


def test_running_out_of_credits_mid_reel_falls_back_and_stops_asking(tmp_path, monkeypatch):
    spoken = []
    monkeypatch.setattr(voices, "edge_tts_voice",
                        lambda text, voice, rate, path, *a: spoken.append(text) or path.write_bytes(b"EDGE") or [])

    def handler(request):
        if request.url.path.endswith("/user/subscription"):
            return httpx.Response(200, json={"character_limit": 30000, "character_count": 0})
        return httpx.Response(401, json={"detail": {"status": "quota_exceeded"}})

    c = client(handler)
    speak = voices.pick_tts(POST, ENV, http=c)
    speak("one", "v", "+20%", tmp_path / "1.mp3")
    speak("two", "v", "+20%", tmp_path / "2.mp3")
    assert spoken == ["one", "two"] and (tmp_path / "2.mp3").read_bytes() == b"EDGE"
    assert sum(1 for m, p, b in c.calls if "text-to-speech" in p) == 1  # asked once, then the free voice
