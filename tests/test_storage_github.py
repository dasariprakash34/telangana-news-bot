"""GitHub-hosted image storage: no network, httpx.MockTransport only."""
import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from tgnews import cli, storage_github
from tgnews.config import ConfigError
from tgnews.storage import StorageError, build_uploader
from tgnews.storage_github import GithubImageStore, check_public_url

TOKEN = "github_pat_SECRETVALUE123456"
NOW = datetime(2026, 10, 28, 9, 30, 15, tzinfo=timezone.utc)
JPEG = b"\xff\xd8\xff\xe0fakejpegbytes"


class Gh:
    """Fake GitHub Contents API recording requests."""

    def __init__(self, files=None, put_status=201, list_status=200, delete_status=200, raise_exc=None,
                 root_status=404, raw_listing=None):
        self.root_status, self.raw_listing = root_status, raw_listing
        self.requests = []
        self.files = dict(files or {})  # path -> sha
        self.put_status = put_status
        self.list_status = list_status
        self.delete_status = delete_status
        self.raise_exc = raise_exc

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_exc:
            raise self.raise_exc
        path = request.url.path.removeprefix("/repos/own/imgs/contents").lstrip("/")
        if request.method == "PUT":
            if path in self.files and self.put_status == 201:
                return httpx.Response(422, json={"message": "\"sha\" wasn't supplied."})
            if self.put_status == 201:
                self.files[path] = "sha-" + path
            return httpx.Response(self.put_status, json={"message": f"msg {TOKEN}"})
        if request.method == "GET":
            if path == "":
                return httpx.Response(self.root_status, json=[])
            if self.list_status != 200:
                return httpx.Response(self.list_status, json={"message": "Not Found"})
            if self.raw_listing is not None:
                return httpx.Response(200, **self.raw_listing)
            prefix = path.rstrip("/") + "/"
            out, seen = [], set()
            for p, sha in sorted(self.files.items()):
                if not p.startswith(prefix):
                    continue
                rest = p[len(prefix):]
                head = rest.split("/")[0]
                if "/" in rest:
                    if head not in seen:
                        seen.add(head)
                        out.append({"name": head, "path": prefix + head, "type": "dir", "sha": "d"})
                else:
                    out.append({"name": head, "path": p, "type": "file", "sha": sha})
            return httpx.Response(200, json=out)
        if request.method == "DELETE":
            body = json.loads(request.content)
            if self.delete_status == 200:
                assert self.files[path] == body["sha"]
                del self.files[path]
            return httpx.Response(self.delete_status, json={"message": "x"})
        raise AssertionError(request.method)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def puts(self):
        return [r for r in self.requests if r.method == "PUT"]

    def deletes(self):
        return [r for r in self.requests if r.method == "DELETE"]


def store(gh, **kw):
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("clock", lambda: NOW)
    kw.setdefault("unique", lambda: "ab12cd")
    return GithubImageStore(TOKEN, "own/imgs", http=gh.client(), **kw)


@pytest.fixture
def jpgs(tmp_path):
    out = []
    for i in range(1, 4):
        p = tmp_path / f"carousel-x-{i}.jpg"
        p.write_bytes(JPEG + bytes([i]))
        out.append(p)
    return out


def test_upload_three_slides_puts_correct_requests(jpgs):
    gh = Gh()
    urls = store(gh).upload(jpgs, "2026-10-28/story1")
    puts = gh.puts()
    assert len(puts) == 3
    for n, (req, p) in enumerate(zip(puts, jpgs), start=1):
        path = f"posts/20261028/2026-10-28-story1-ab12cd-{n}.jpg"
        assert str(req.url) == f"https://api.github.com/repos/own/imgs/contents/{path}"
        assert req.headers["authorization"] == f"Bearer {TOKEN}"
        assert req.headers["accept"] == "application/vnd.github+json"
        assert req.headers["x-github-api-version"] == "2022-11-28"
        body = json.loads(req.content)
        assert set(body) == {"message", "content", "branch"}
        assert body["branch"] == "main"
        assert base64.b64decode(body["content"]) == p.read_bytes()
        assert body["message"]
    assert urls == [f"https://raw.githubusercontent.com/own/imgs/main/posts/20261028/2026-10-28-story1-ab12cd-{n}.jpg"
                    for n in (1, 2, 3)]


def test_public_base_override_and_https_only(jpgs):
    gh = Gh()
    s = store(gh, branch="live", public_base="https://cdn.jsdelivr.net/gh/own/imgs@live/")
    urls = s.upload(jpgs[:1], "p")
    assert urls == ["https://cdn.jsdelivr.net/gh/own/imgs@live/posts/20261028/p-ab12cd-1.jpg"]
    assert json.loads(gh.puts()[0].content)["branch"] == "live"
    with pytest.raises(StorageError, match="https"):
        GithubImageStore(TOKEN, "own/imgs", public_base="http://x.example")


def test_paths_unique_per_post_and_per_call(jpgs):
    import itertools
    counter = itertools.count()
    gh = Gh()
    s = GithubImageStore(TOKEN, "own/imgs", http=gh.client(), sleep=lambda x: None, clock=lambda: NOW,
                         unique=lambda: f"u{next(counter)}")
    a = s.upload(jpgs, "2026-10-28/story1")
    b = s.upload(jpgs, "2026-10-28/story1")
    c = s.upload(jpgs, "2026-10-28/story2")
    assert len(set(a + b + c)) == 9


def test_hostile_prefix_is_sanitised(jpgs):
    gh = Gh()
    store(gh).upload(jpgs[:1], "../../etc/passwd x")
    path = gh.puts()[0].url.path
    assert ".." not in path and path.startswith("/repos/own/imgs/contents/posts/20261028/")
    with pytest.raises(StorageError):
        store(Gh()).upload(jpgs[:1], "///")


def test_sleeps_between_files_only(jpgs):
    sleeps = []
    store(Gh(), sleep=sleeps.append).upload(jpgs, "p")
    assert len(sleeps) == 2


def test_already_exists_is_error_never_overwrite(jpgs):
    existing = "posts/20261028/p-ab12cd-1.jpg"
    gh = Gh(files={existing: "oldsha"})
    with pytest.raises(StorageError, match="not overwr"):
        store(gh).upload(jpgs, "p")
    assert gh.files[existing] == "oldsha"
    assert len(gh.puts()) == 2  # one retry with a fresh random part, then stop
    assert "sha" not in json.loads(gh.puts()[0].content)


@pytest.mark.parametrize("status", [409, 422])
def test_409_422_map_to_exists(jpgs, status):
    with pytest.raises(StorageError, match="not overwr"):
        store(Gh(put_status=status)).upload(jpgs[:1], "p")


def test_200_response_means_an_update_and_is_an_error(jpgs):
    with pytest.raises(StorageError, match="unexpected"):
        store(Gh(put_status=200)).upload(jpgs[:1], "p")


@pytest.mark.parametrize("status,needle", [(401, "token"), (403, "permission"), (404, "not found"),
                                           (500, "GitHub"), (503, "GitHub"), (429, "rate")])
def test_status_mapping(jpgs, status, needle):
    with pytest.raises(StorageError) as ei:
        store(Gh(put_status=status)).upload(jpgs[:1], "p")
    assert needle.lower() in str(ei.value).lower()
    assert TOKEN not in str(ei.value) and "msg" not in str(ei.value)
    assert ei.value.__cause__ is None


def test_network_error_mapped_without_token(jpgs):
    exc = httpx.ConnectError(f"boom {TOKEN}")
    with pytest.raises(StorageError) as ei:
        store(Gh(raise_exc=exc)).upload(jpgs[:1], "p")
    assert TOKEN not in str(ei.value) and "ConnectError" in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


def test_token_hidden_in_repr_and_logs(jpgs, caplog):
    s = store(Gh())
    assert TOKEN not in repr(s) and TOKEN not in str(s)
    assert "own/imgs" in repr(s)
    with caplog.at_level(logging.DEBUG):
        s.upload(jpgs[:1], "p")
    assert TOKEN not in caplog.text
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_invalid_files_rejected_before_any_request(tmp_path, jpgs):
    png = tmp_path / "a.png"
    png.write_bytes(b"x")
    big = tmp_path / "big.jpg"
    big.write_bytes(b"\0" * (5 * 1024 * 1024 + 1))
    for bad in (png, tmp_path / "missing.jpg", big, tmp_path):
        gh = Gh()
        with pytest.raises(StorageError):
            store(gh).upload([jpgs[0], bad], "p")
        assert gh.requests == []


def test_bad_repo_and_branch_rejected():
    for repo in ("noslash", "a/b/c", "", "a/ b", "../x"):
        with pytest.raises(StorageError) as ei:
            GithubImageStore(TOKEN, repo)
        assert TOKEN not in str(ei.value)
    with pytest.raises(StorageError):
        GithubImageStore(TOKEN, "a/b", branch="x..y")
    with pytest.raises(StorageError):
        GithubImageStore("", "a/b")


# ---------------------------------------------------------------- prune

def seeded(*paths):
    return Gh(files={p: "sha-" + p for p in paths})


def test_prune_deletes_only_old_posts_dirs_using_sha():
    gh = seeded("posts/20261001/a-1.jpg", "posts/20261001/a-2.jpg",
                "posts/20261014/b-1.jpg",   # exactly 14 days old: kept
                "posts/20261020/c-1.jpg",
                "posts/notadate/x.jpg", "posts/2026100/x.jpg", "posts/20261340/x.jpg",
                "posts/20260901.jpg", "README.md", "other/20260101/x.jpg")
    n = store(gh).prune(14, NOW).deleted
    assert n == 2
    assert sorted(gh.files) == sorted(["posts/20261014/b-1.jpg", "posts/20261020/c-1.jpg",
                                       "posts/notadate/x.jpg", "posts/2026100/x.jpg",
                                       "posts/20261340/x.jpg", "posts/20260901.jpg", "README.md",
                                       "other/20260101/x.jpg"])
    for d in gh.deletes():
        body = json.loads(d.content)
        assert set(body) == {"message", "sha", "branch"} and body["sha"].startswith("sha-")
        assert d.url.path.startswith("/repos/own/imgs/contents/posts/20261001/")
        assert d.headers["authorization"] == f"Bearer {TOKEN}"
    assert all("/contents/other" not in r.url.path and "/contents/README" not in r.url.path
               for r in gh.requests)


def test_prune_respects_per_run_bound_oldest_first():
    gh = seeded(*[f"posts/20260901/f{i}.jpg" for i in range(5)], "posts/20260902/g.jpg")
    n = store(gh).prune(14, NOW, max_files=3).deleted
    assert n == 3 and len(gh.deletes()) == 3
    assert "posts/20260902/g.jpg" in gh.files and len(gh.files) == 3
    assert store(gh).prune(14, NOW, max_files=60).deleted == 3 and gh.files == {}


def test_prune_default_bound_is_60():
    gh = seeded(*[f"posts/20260901/f{i:03}.jpg" for i in range(70)])
    assert store(gh).prune(14, NOW).deleted == 60


def test_prune_tolerates_missing_posts_dir_and_deleted_file():
    gh = seeded("README.md")  # repo exists, posts/ does not yet
    gh.root_status = 200
    assert store(gh).prune(14, NOW) == storage_github.PruneResult(0, 0)
    gh = seeded("posts/20260901/a.jpg")
    gh.delete_status = 404
    assert store(gh).prune(14, NOW) == storage_github.PruneResult(0, 1)


def test_prune_errors_map_without_token_and_listing_404_means_repo_problem_unless_root_ok():
    with pytest.raises(StorageError, match="not found"):  # posts/ 404 and repo root 404 as well
        store(Gh(list_status=404, root_status=404)).prune(14, NOW)
    assert store(Gh(list_status=404, root_status=200)).prune(14, NOW).deleted == 0
    for st in (401, 403):
        with pytest.raises(StorageError):
            store(Gh(list_status=404, root_status=st)).prune(14, NOW)
    gh = Gh(list_status=404, root_status=200)
    store(gh).prune(14, NOW)
    assert len(gh.requests) == 2  # exactly one extra request


def test_prune_errors_map_without_token():
    with pytest.raises(StorageError) as ei:
        store(Gh(list_status=401)).prune(14, NOW)
    assert TOKEN not in str(ei.value)


def test_prune_never_touches_future_or_zero_arg_misuse():
    gh = seeded("posts/20271001/a.jpg")
    assert store(gh).prune(14, NOW).deleted == 0
    for days in (0, 1, 2):
        with pytest.raises(ConfigError):
            store(gh).prune(days, NOW)


# ---------------------------------------------------------------- check_public_url

def test_check_public_url_reports_status_and_type():
    def h(req):
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"x")
    http = httpx.Client(transport=httpx.MockTransport(h))
    r = check_public_url("https://raw.githubusercontent.com/o/r/main/x.jpg", http=http)
    assert (r.status, r.content_type, r.ok) == (200, "image/jpeg", True)
    http = httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(404)))
    assert check_public_url("https://x.example/a.jpg", http=http).ok is False
    http = httpx.Client(transport=httpx.MockTransport(
        lambda q: httpx.Response(200, headers={"content-type": "text/plain"})))
    assert check_public_url("https://x.example/a.jpg", http=http).ok is False
    with pytest.raises(StorageError, match="https"):
        check_public_url("http://x.example/a.jpg", http=http)


def test_check_public_url_network_error():
    def h(req):
        raise httpx.ConnectError("nope")
    with pytest.raises(StorageError, match="ConnectError"):
        check_public_url("https://x.example/a.jpg", http=httpx.Client(transport=httpx.MockTransport(h)))


# ---------------------------------------------------------------- build_uploader / env

GH_ENV = {"IMAGES_GH_TOKEN": TOKEN, "IMAGES_GH_REPO": "own/imgs"}
S3_ENV = {"STORAGE_PROVIDER": "s3", "STORAGE_ENDPOINT": "https://e.example", "STORAGE_BUCKET": "b",
          "STORAGE_PUBLIC_BASE": "https://pub.example", "STORAGE_ACCESS_KEY_ID": "AK1234",
          "STORAGE_SECRET_ACCESS_KEY": "SK1234"}


def test_default_provider_is_github(monkeypatch, jpgs):
    gh = Gh()
    monkeypatch.setattr(storage_github, "_default_http", gh.client)
    monkeypatch.setattr(storage_github.time, "sleep", lambda s: None)
    up = build_uploader({**GH_ENV, "IMAGES_GH_BRANCH": "pics", "IMAGES_PUBLIC_BASE": "https://cdn.example/g"})
    urls = up(jpgs[:1], "2026-10-28/s")
    assert urls[0].startswith("https://cdn.example/g/posts/")
    assert json.loads(gh.puts()[0].content)["branch"] == "pics"


def test_empty_provider_means_github():
    assert callable(build_uploader({**GH_ENV, "STORAGE_PROVIDER": " "}))


def test_s3_provider_selected(monkeypatch, jpgs):
    from tgnews import storage
    seen = {}
    monkeypatch.setattr(storage, "upload", lambda paths, prefix, *, client=None, config=None:
                        seen.update(cfg=config, prefix=prefix) or ["u"])
    up = build_uploader(S3_ENV)
    assert up(jpgs, "pre") == ["u"] and seen["cfg"].bucket == "b" and seen["prefix"] == "pre"


@pytest.mark.parametrize("drop", ["IMAGES_GH_TOKEN", "IMAGES_GH_REPO"])
def test_missing_github_variables(drop):
    env = {k: v for k, v in GH_ENV.items() if k != drop}
    with pytest.raises(StorageError, match=drop) as ei:
        build_uploader(env)
    assert TOKEN not in str(ei.value)


def test_github_does_not_need_s3_variables_and_s3_does_not_need_github():
    build_uploader(GH_ENV)
    build_uploader(S3_ENV)
    with pytest.raises(StorageError, match="STORAGE_"):
        build_uploader({"STORAGE_PROVIDER": "s3"})


def test_unknown_provider_and_bad_keep_days():
    with pytest.raises(StorageError, match="STORAGE_PROVIDER"):
        build_uploader({**GH_ENV, "STORAGE_PROVIDER": "ftp"})
    for bad in ("0", "-1", "abc", "1.5"):
        with pytest.raises(ConfigError, match="IMAGES_KEEP_DAYS"):
            storage_github.keep_days({"IMAGES_KEEP_DAYS": bad})
    assert storage_github.keep_days({}) == 14
    assert storage_github.keep_days({"IMAGES_KEEP_DAYS": "30"}) == 30


def test_bad_public_base_in_env_rejected():
    with pytest.raises(StorageError, match="IMAGES_PUBLIC_BASE|https"):
        build_uploader({**GH_ENV, "IMAGES_PUBLIC_BASE": "http://insecure.example"})


def test_build_deps_uses_build_uploader(monkeypatch):
    # purpose=run wires the uploader from the provider selection
    calls = []
    monkeypatch.setattr(cli, "build_uploader", lambda env: calls.append(env) or "UP")
    monkeypatch.setattr(cli, "build_llm", lambda env: (object(), "m"))
    env = {**GH_ENV, "DATABASE_URL": "sqlite://", "SOURCES_PATH": str(Path(__file__).parent.parent / "config" / "sources.yaml")}
    deps = cli.build_deps(cli.Settings.from_env(env), env, purpose="run")
    assert deps.upload == "UP" and calls


# ---------------------------------------------------------------- CLI commands

def test_cli_prune_images(monkeypatch, capsys):
    gh = seeded("posts/20260901/a.jpg", "posts/20261027/b.jpg")
    monkeypatch.setattr(storage_github, "_default_http", gh.client)
    monkeypatch.setattr(storage_github.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    env = {**GH_ENV, "IMAGES_KEEP_DAYS": "7"}
    assert cli.main(["prune-images"], env) == 0
    assert "deleted=1 skipped=0" in capsys.readouterr().out
    assert list(gh.files) == ["posts/20261027/b.jpg"]


def test_cli_prune_images_skipped_for_s3(capsys):
    assert cli.main(["prune-images"], S3_ENV) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_cli_prune_images_error_is_scrubbed(monkeypatch, capsys):
    gh = Gh(list_status=401)
    monkeypatch.setattr(storage_github, "_default_http", gh.client)
    assert cli.main(["prune-images"], GH_ENV) == 1
    err = capsys.readouterr().err
    assert TOKEN not in err and "token" in err.lower()


def test_cli_check_images(monkeypatch, capsys):
    def h(req):
        return httpx.Response(200, headers={"content-type": "image/jpeg"})
    monkeypatch.setattr(storage_github, "_default_http", lambda: httpx.Client(transport=httpx.MockTransport(h)))
    assert cli.main(["check-images", "https://x.example/a.jpg"], {}) == 0
    out = capsys.readouterr().out
    assert "200" in out and "image/jpeg" in out
    monkeypatch.setattr(storage_github, "_default_http",
                        lambda: httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(404))))
    assert cli.main(["check-images", "https://x.example/a.jpg"], {}) == 1


# ---------------------------------------------------------------- review fixes

def test_prune_bounded_by_attempts_when_delete_keeps_failing():
    gh = seeded(*[f"posts/20260901/f{i:03}.jpg" for i in range(100)])
    gh.delete_status = 422
    sleeps = []
    res = store(gh, sleep=sleeps.append).prune(14, NOW)
    assert res == storage_github.PruneResult(0, 60)
    assert len(gh.deletes()) == 60 and len(sleeps) == 60
    assert len(gh.requests) <= 2 + 60


def test_prune_day_boundaries():
    gh = seeded("posts/20261013/old.jpg", "posts/20261014/edge.jpg")
    assert store(gh).prune(14, NOW).deleted == 1  # day 15 goes, day 14 stays
    assert list(gh.files) == ["posts/20261014/edge.jpg"]
    for moment in (datetime(2026, 10, 28, 0, 0, 0, tzinfo=timezone.utc),
                   datetime(2026, 10, 28, 23, 59, 59, tzinfo=timezone.utc)):
        gh = seeded("posts/20261013/old.jpg", "posts/20261014/edge.jpg")
        assert store(gh).prune(14, moment).deleted == 1
        assert list(gh.files) == ["posts/20261014/edge.jpg"]


@pytest.mark.parametrize("raw", [{"json": {"message": "oops"}}, {"content": b"<html>nope</html>"},
                                 {"json": "text"}])
def test_prune_rejects_non_list_listing(raw):
    gh = Gh(raw_listing=raw)
    with pytest.raises(StorageError, match="unexpected") as ei:
        store(gh).prune(14, NOW)
    assert "oops" not in str(ei.value) and "html" not in str(ei.value)
    assert gh.deletes() == []


def test_collision_retries_once_with_fresh_random_part(jpgs):
    import itertools
    gh = Gh(files={"posts/20261028/p-r0-1.jpg": "x"})
    counter = itertools.count()
    s = GithubImageStore(TOKEN, "own/imgs", http=gh.client(), sleep=lambda x: None, clock=lambda: NOW,
                         unique=lambda: f"r{next(counter)}")
    urls = s.upload(jpgs[:2], "p")
    assert [u.rsplit("/", 1)[1] for u in urls] == ["p-r1-1.jpg", "p-r1-2.jpg"]
    assert len(gh.puts()) == 3 and gh.files["posts/20261028/p-r0-1.jpg"] == "x"


def test_persistent_rejection_message_is_generic(jpgs):
    with pytest.raises(StorageError) as ei:
        store(Gh(put_status=422)).upload(jpgs[:1], "p")
    assert "HTTP 422" in str(ei.value) and "already exists" not in str(ei.value)
    gh = Gh(put_status=422)
    with pytest.raises(StorageError):
        store(gh).upload(jpgs[:1], "p")
    assert len(gh.puts()) == 2


@pytest.mark.parametrize("repo", ["./x", "x/..", "../..", "a/.", "a/...", "./.."])
def test_repo_segments_of_dots_rejected(repo):
    with pytest.raises(StorageError):
        GithubImageStore(TOKEN, repo)


@pytest.mark.parametrize("base", ["https://c.example/x?a=1", "https://c.example/x#f", "https://c.example/a b"])
def test_public_base_with_query_or_fragment_rejected(base):
    with pytest.raises(StorageError):
        GithubImageStore(TOKEN, "a/b", public_base=base)


@pytest.mark.parametrize("branch", ["a//b", "-x", "x/", "foo.lock", "a..b", "/x", ".x", "x.", "a b", "a/.b"])
def test_bad_branches_rejected(branch):
    with pytest.raises(StorageError):
        GithubImageStore(TOKEN, "a/b", branch=branch)
    for ok in ("main", "release/1.0", "feature_x-1"):
        GithubImageStore(TOKEN, "a/b", branch=ok)


def test_non_jpeg_content_rejected_before_any_request(tmp_path, jpgs):
    fake = tmp_path / "fake.jpg"
    fake.write_bytes(b"GIF89a....")
    gh = Gh()
    with pytest.raises(StorageError, match="JPEG"):
        store(gh).upload([jpgs[0], fake], "p")
    assert gh.requests == []


def test_storage_provider_single_validator():
    from tgnews.storage import storage_provider
    assert storage_provider({}) == "github" and storage_provider({"STORAGE_PROVIDER": " S3 "}) == "s3"
    with pytest.raises(StorageError, match="STORAGE_PROVIDER"):
        storage_provider({"STORAGE_PROVIDER": "r2"})


def test_cli_images_commands_ignore_unrelated_bad_settings(monkeypatch, capsys):
    gh = seeded()
    gh.root_status = 200
    monkeypatch.setattr(storage_github, "_default_http", gh.client)
    env = {**GH_ENV, "MODE": "bogus", "DAILY_CAP": "x"}
    assert cli.main(["prune-images"], env) == 0
    monkeypatch.setattr(storage_github, "_default_http", lambda: httpx.Client(
        transport=httpx.MockTransport(lambda q: httpx.Response(200, headers={"content-type": "image/jpeg"}))))
    assert cli.main(["check-images", "https://x.example/a.jpg"], env) == 0


def test_cli_prune_unknown_provider_is_an_error(capsys):
    assert cli.main(["prune-images"], {"STORAGE_PROVIDER": "r2"}) == 1
    err = capsys.readouterr().err
    assert "STORAGE_PROVIDER" in err


def test_cli_keep_days_below_minimum_rejected(capsys):
    assert cli.main(["prune-images"], {**GH_ENV, "IMAGES_KEEP_DAYS": "2"}) == 1
    err = capsys.readouterr().err
    assert "IMAGES_KEEP_DAYS" in err and TOKEN not in err
    with pytest.raises(ConfigError) as ei:
        storage_github.keep_days({"IMAGES_KEEP_DAYS": "2"})
    assert str(ei.value) == "IMAGES_KEEP_DAYS must be a whole number of at least 3"


class _Boom(httpx.SyncByteStream):
    def __iter__(self):
        raise AssertionError("body must not be read")
        yield b""


def test_check_url_reads_headers_only_and_sends_no_credentials():
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, stream=_Boom())
    r = check_public_url("https://x.example/a.jpg", http=httpx.Client(transport=httpx.MockTransport(h)))
    assert r.ok and "authorization" not in seen[0].headers
