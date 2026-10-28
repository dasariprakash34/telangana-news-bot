from pathlib import Path

import pytest

from tgnews.storage import StorageConfig, StorageError, upload

ENV = {
    "STORAGE_ENDPOINT": "https://acct.r2.example.com",
    "STORAGE_BUCKET": "cards",
    "STORAGE_PUBLIC_BASE": "https://pub.example.com/",
    "STORAGE_ACCESS_KEY_ID": "AKID-SECRET-1",
    "STORAGE_SECRET_ACCESS_KEY": "SHHH-SECRET-2",
}


class StubClient:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def put_object(self, **kw):
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise RuntimeError("boom AKID-SECRET-1")
        self.calls.append(kw)


@pytest.fixture
def cfg():
    return StorageConfig.from_env(ENV)


def _files(tmp_path, *names):
    out = []
    for n in names:
        p = tmp_path / n
        p.write_bytes(b"\xff\xd8jpeg")
        out.append(p)
    return out


def test_upload_returns_public_urls_in_order(tmp_path, cfg):
    paths = _files(tmp_path, "b.jpg", "a.jpg", "c.jpeg")
    client = StubClient()
    urls = upload(paths, "/posts/42/", client=client, config=cfg)
    assert urls == [
        "https://pub.example.com/posts/42/b.jpg",
        "https://pub.example.com/posts/42/a.jpg",
        "https://pub.example.com/posts/42/c.jpeg",
    ]
    assert [c["Key"] for c in client.calls] == ["posts/42/b.jpg", "posts/42/a.jpg", "posts/42/c.jpeg"]


def test_upload_failure_raises_StorageError(tmp_path, cfg):
    paths = _files(tmp_path, "a.jpg", "b.jpg")
    client = StubClient(fail_on=1)
    with pytest.raises(StorageError) as ei:
        upload(paths, "p", client=client, config=cfg)
    assert isinstance(ei.value.__cause__, RuntimeError)
    assert "AKID-SECRET-1" not in str(ei.value)
    assert len(client.calls) == 1


def test_content_type_and_cache_control(tmp_path, cfg):
    client = StubClient()
    upload(_files(tmp_path, "a.jpg"), "p", client=client, config=cfg)
    c = client.calls[0]
    assert c["Bucket"] == "cards"
    assert c["ContentType"] == "image/jpeg"
    assert c["CacheControl"] == "public, max-age=31536000, immutable"
    assert c["Body"] == b"\xff\xd8jpeg"


def test_key_quoting(tmp_path, cfg):
    client = StubClient()
    urls = upload(_files(tmp_path, "a b#1?.jpg"), "p q", client=client, config=cfg)
    assert urls == ["https://pub.example.com/p%20q/a%20b%231%3F.jpg"]


@pytest.mark.parametrize("prefix", ["../x", "a/../b", "", "///"])
def test_bad_prefix_rejected(tmp_path, cfg, prefix):
    with pytest.raises(StorageError):
        upload(_files(tmp_path, "a.jpg"), prefix, client=StubClient(), config=cfg)


def test_non_jpeg_rejected(tmp_path, cfg):
    client = StubClient()
    with pytest.raises(StorageError):
        upload(_files(tmp_path, "a.png"), "p", client=client, config=cfg)
    assert client.calls == []


def test_missing_file_rejected(tmp_path, cfg):
    with pytest.raises(StorageError):
        upload([tmp_path / "nope.jpg"], "p", client=StubClient(), config=cfg)


def test_config_from_env_defaults():
    c = StorageConfig.from_env(ENV)
    assert c.region == "auto" and c.bucket == "cards"
    assert StorageConfig.from_env({**ENV, "STORAGE_REGION": "us-east-1"}).region == "us-east-1"


@pytest.mark.parametrize("missing", [k for k in ENV])
def test_config_missing_variable_named_without_secrets(missing):
    env = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(StorageError) as ei:
        StorageConfig.from_env(env)
    msg = str(ei.value)
    assert missing in msg
    assert "AKID-SECRET-1" not in msg and "SHHH-SECRET-2" not in msg


def test_non_https_public_base_rejected():
    with pytest.raises(StorageError):
        StorageConfig.from_env({**ENV, "STORAGE_PUBLIC_BASE": "http://pub.example.com"})
