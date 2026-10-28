from pathlib import Path

import pytest
from PIL import Image, ImageChops, features

from tgnews import render
from tgnews.render import (
    MARGIN,
    ShapingUnavailable,
    assert_shaping_available,
    render_card,
    render_carousel,
)
from tgnews.rewrite import RewrittenPost

POST = RewrittenPost(
    headline_te="హైదరాబాద్‌లో భారీ వర్షం, రోడ్లు జలమయం",
    points_te=[
        "సోమవారం నగరంలో భారీ వర్షం కురిసింది",
        "పలు రోడ్లు నీట మునిగాయి",
        "ప్రజలు అప్రమత్తంగా ఉండాలని అధికారులు సూచించారు",
        "రేపు కూడా వర్షాలు కొనసాగే అవకాశం ఉంది",
    ],
    caption_te="క్యాప్షన్",
    hashtags=["#Hyderabad"],
    source_credit="మూలం: The Hindu",
    topic="weather",
    headline_style="statement",
)


def test_card_is_1080x1350_jpeg(tmp_path):
    paths = render_card(POST, tmp_path)
    assert len(paths) == 1
    with Image.open(paths[0]) as im:
        assert im.format == "JPEG"
        assert im.size == (1080, 1350)


def test_carousel_slide_count_between_3_and_5(tmp_path):
    for n in (1, 2, 3, 4):
        post = RewrittenPost(**{**POST.__dict__, "points_te": POST.points_te[:n] if n > 1 else POST.points_te[:1]})
        paths = render_carousel(post, tmp_path / str(n))
        assert 3 <= len(paths) <= 5
        for p in paths:
            with Image.open(p) as im:
                assert im.format == "JPEG" and im.size == (1080, 1350)


def test_long_headline_fits_inside_margins(tmp_path, monkeypatch):
    bg = (30, 60, 90)
    monkeypatch.setitem(render.PALETTE, "other", render.Theme(bg, bg, (255, 255, 255), (255, 255, 255)))
    headline = ("తెలంగాణ రాష్ట్రంలో ప్రభుత్వం కొత్త పథకాన్ని ప్రారంభించింది " * 3)[:90]
    assert len(headline) == 90
    post = RewrittenPost(**{**POST.__dict__, "headline_te": headline, "topic": "other"})
    (path,) = render_card(post, tmp_path)
    with Image.open(path) as im:
        rgb = im.convert("RGB")
    # outside the margin box everything must equal the background (JPEG tolerance)
    mask = Image.new("L", rgb.size, 255)
    mask.paste(0, (MARGIN, MARGIN, 1080 - MARGIN, 1350 - MARGIN))
    flat = Image.new("RGB", rgb.size, bg)
    diff = ImageChops.difference(rgb, flat).convert("L")
    outside = ImageChops.multiply(diff, mask)
    assert outside.getextrema()[1] <= 6
    # and something was actually drawn inside the box
    inside_mask = Image.new("L", rgb.size, 0)
    inside_mask.paste(255, (MARGIN, MARGIN, 1080 - MARGIN, 1350 - MARGIN))
    inside = ImageChops.multiply(diff, inside_mask)
    assert inside.getextrema()[1] > 100


def test_missing_raqm_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(features, "check", lambda name: False)
    with pytest.raises(ShapingUnavailable):
        assert_shaping_available()
    with pytest.raises(RuntimeError):
        render_card(POST, tmp_path)
    with pytest.raises(ShapingUnavailable):
        render_carousel(POST, tmp_path)
    assert not list(tmp_path.glob("*.jpg"))


def test_filenames_deterministic(tmp_path):
    a = render_card(POST, tmp_path / "a")
    b = render_card(POST, tmp_path / "b")
    assert a[0].name == b[0].name
    assert a[0].name.startswith("card-") and a[0].suffix == ".jpg"
    c = render_carousel(POST, tmp_path / "c")
    d = render_carousel(POST, tmp_path / "d")
    assert [p.name for p in c] == [p.name for p in d]
    assert c[0].name.startswith("carousel-") and c[0].name.endswith("-1.jpg")
    other = RewrittenPost(**{**POST.__dict__, "headline_te": "మరో శీర్షిక"})
    assert render_card(other, tmp_path / "e")[0].name != a[0].name


def test_brand_is_parameter(tmp_path):
    a = render_card(POST, tmp_path / "a", brand="Telangana News")[0]
    b = render_card(POST, tmp_path / "b", brand="Other Page")[0]
    with Image.open(a) as ia, Image.open(b) as ib:
        assert ImageChops.difference(ia.convert("RGB"), ib.convert("RGB")).getbbox() is not None
