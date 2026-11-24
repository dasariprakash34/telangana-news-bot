from pathlib import Path

from tgnews import photo, reel_anim
from tgnews.rewrite import RewrittenPost

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100


def page(title, licence="CC BY-SA 4.0", width=2000, mime="image/jpeg", index=1, artist="<a>Ravi</a>"):
    return {"title": title, "index": index, "imageinfo": [{
        "thumburl": f"https://upload.example/{index}.jpg", "width": width, "mime": mime,
        "extmetadata": {"LicenseShortName": {"value": licence}, "Artist": {"value": artist}}}]}


def pages(*ps):
    return {"query": {"pages": {str(i): p for i, p in enumerate(ps)}}}


def post(**kw):
    base = dict(headline_te="వార్త", points_te=["ఒకటి", "రెండు"], caption_te="క్యాప్షన్.", hashtags=[],
                source_credit="", topic="other", headline_style="statement")
    return RewrittenPost(**{**base, **kw})


def test_picks_a_free_named_place_photo_and_skips_shops_maps_and_unfree(tmp_path):
    result = pages(page("File:Khans online service vikarabad.jpg", index=1),
                   page("File:Vikarabad district map.png", mime="image/png", index=2),
                   page("File:Vikarabad fort.jpg", licence="All rights reserved", index=3),
                   page("File:Tiny vikarabad.jpg", width=400, index=4),
                   page("File:Rural Road near Vikarabad.jpg", licence="CC BY 2.0", index=5))
    got = photo.find_photo("Vikarabad", "other", tmp_path, get_json=lambda u: result, get_bytes=lambda u: JPEG)
    assert got.title == "File:Rural Road near Vikarabad.jpg"
    assert got.credit == "ఫోటో: Ravi, CC BY 2.0, Wikimedia Commons"
    assert got.path.read_bytes() == JPEG


def test_no_photo_for_crime_or_without_a_query_and_never_raises(tmp_path):
    def boom(url):
        raise OSError("offline")
    assert photo.find_photo("Vikarabad", "crime", tmp_path, get_json=boom) is None
    assert photo.find_photo("", "other", tmp_path, get_json=boom) is None
    assert photo.find_photo("Vikarabad", "other", tmp_path, get_json=boom) is None
    html_not_image = lambda u: b"<html>"
    assert photo.find_photo("Vikarabad", "other", tmp_path, get_json=lambda u: pages(page("File:Vikarabad.jpg")),
                            get_bytes=html_not_image) is None


def test_attach_credits_the_caption_and_marks_the_design(tmp_path):
    p = post(design="scene")
    pic = photo.Photo(path=tmp_path / "photo.jpg", credit="ఫోటో: Ravi, CC BY 2.0, Wikimedia Commons", title="t")
    assert photo.attach(p, pic) and p.design == "photo" and p.photo_path.endswith("photo.jpg")
    assert p.caption_te.endswith("\n\nఫోటో: Ravi, CC BY 2.0, Wikimedia Commons")
    photo.attach(p, pic)  # idempotent: the credit is not added twice
    assert p.caption_te.count("Wikimedia") == 1
    assert photo.attach(post(design="scene"), None) is False


def test_reel_page_gets_the_photo_and_on_screen_credit(tmp_path):
    (tmp_path / "photo.jpg").write_bytes(JPEG)
    p = post(photo_path=str(tmp_path / "photo.jpg"), photo_credit="ఫోటో: Ravi, CC BY 2.0, Wikimedia Commons")
    meta = reel_anim.meta_for(p, "Telangana News")
    assert meta["photo"].startswith("file://") and "Ravi, CC BY 2.0" in meta["foot"]
    assert "photo" not in reel_anim.meta_for(post(), "Telangana News")


def test_up_to_three_photos_of_the_place_with_every_author_credited(tmp_path):
    result = pages(page("File:Tirumala 1.jpg", index=1, artist="A"),
                   page("File:Tirumala 2.jpg", index=2, artist="B", licence="CC BY 3.0"),
                   page("File:Tirumala 3.jpg", index=3, artist="A"),
                   page("File:Tirumala 4.jpg", index=4, artist="C"))
    got = photo.find_photo("Tirumala", "temples", tmp_path, get_json=lambda u: result, get_bytes=lambda u: JPEG)
    assert got.path.name == "photo-1.jpg" and [p.name for p in got.more] == ["photo-2.jpg", "photo-3.jpg"]
    assert got.credit == "ఫోటోలు: A (CC BY-SA 4.0); B (CC BY 3.0), Wikimedia Commons"
    p = post()
    photo.attach(p, got)
    meta = reel_anim.meta_for(p, "Telangana News")
    assert len(meta["photos"]) == 3 and meta["photos"][0] == meta["photo"]
