import pytest

from tgnews.telugu_numbers import speakable, words


@pytest.mark.parametrize("n, expected", [
    (0, "సున్నా"), (7, "ఏడు"), (15, "పదిహేను"), (21, "ఇరవై ఒకటి"), (99, "తొంభై తొమ్మిది"),
    (100, "వంద"), (101, "నూట ఒకటి"), (200, "రెండు వందలు"), (205, "రెండు వందల ఐదు"),
    (1000, "వెయ్యి"), (1005, "వెయ్యి ఐదు"), (2026, "రెండు వేల ఇరవై ఆరు"), (25000, "ఇరవై ఐదు వేలు"),
    (100000, "లక్ష"), (125000, "లక్ష ఇరవై ఐదు వేలు"), (2000000, "ఇరవై లక్షలు"), (10000000, "కోటి"),
])
def test_whole_numbers(n, expected):
    assert words(n) == expected


def test_the_price_that_was_read_wrongly():
    assert speakable("ధర 25,000 రూపాయలు") == "ధర ఇరవై ఐదు వేల రూపాయలు"
    assert speakable("ధర ₹25,000") == "ధర ఇరవై ఐదు వేల రూపాయలు"
    assert speakable("Rs. 300 టికెట్") == "మూడు వందల రూపాయలు టికెట్"


def test_the_plural_ends_before_a_noun_and_stays_at_the_end():
    assert speakable("5,000 మంది భక్తులు") == "ఐదు వేల మంది భక్తులు"
    assert speakable("ఖరీదు 25,000") == "ఖరీదు ఇరవై ఐదు వేలు"


def test_dates_times_percent_and_decimals():
    assert speakable("06.11.2026") == "ఆరు నవంబర్ రెండు వేల ఇరవై ఆరు"
    assert speakable("3:30 PM") == "మధ్యాహ్నం మూడు గంటల ముప్పై నిమిషాలకు"
    assert speakable("5 AM") == "ఉదయం ఐదు గంటలకు"
    assert speakable("7 PM") == "రాత్రి ఏడు గంటలకు" and speakable("5 PM") == "సాయంత్రం ఐదు గంటలకు"
    assert speakable("10 శాతం") == "పది శాతం" and speakable("12.5%") == "పన్నెండు పాయింట్ ఐదు శాతం"


def test_telugu_digits_work_too_and_unclear_things_are_left_alone():
    assert speakable("౨౫,౦౦౦") == "ఇరవై ఐదు వేలు"
    assert speakable("5G ఫోన్ T20") == "5G ఫోన్ T20"
    assert words(10 ** 9) == str(10 ** 9)
    assert speakable("") == ""


def test_the_reel_speaks_and_shows_the_spelled_out_numbers():
    from tgnews import reel, reel_anim
    from tgnews.rewrite import RewrittenPost
    post = RewrittenPost(headline_te="ధర 25,000 రూపాయలు", points_te=["టికెట్లు మధ్యాహ్నం 3 PM కి"], caption_te="x",
                         hashtags=[], source_credit="", topic="temples", headline_style="statement")
    assert reel_anim.scenes_for(post, "b")[0]["say"] == "ధర ఇరవై ఐదు వేల రూపాయలు"
    assert reel.narration(post)[0] == "ధర ఇరవై ఐదు వేల రూపాయలు"
