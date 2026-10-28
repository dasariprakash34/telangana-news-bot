# Telangana News Instagram Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A scheduled Python pipeline that fetches Telangana news, rewrites it as original Telugu Instagram posts, publishes via the Instagram Graph API (review or auto mode), and tunes topic, format, time slot and headline style from post insights.

**Architecture:** One package `tgnews` of small single-purpose modules (fetch, dedupe, safety, rewrite, render, storage, gate, publish, analyze, learn, report) wired by a thin `pipeline.py` and a `cli.py`. State lives in a SQL database through SQLAlchemy Core (SQLite in tests, hosted Postgres in production). Scheduled jobs are GitHub Actions workflows.

**Tech Stack:** Python 3.11+, pytest, SQLAlchemy, httpx, feedparser, Pillow (with raqm for Telugu shaping), anthropic SDK, boto3 (S3-compatible storage), python-telegram-bot or plain httpx against the Telegram Bot API.

**Spec:** `docs/superpowers/specs/2026-10-28-telangana-instagram-bot-design.md`

## Global Constraints

- `MODE` is `review` or `auto`; one setting, default `review`.
- Posts are Telugu (headline image and caption) and carry a visible source credit; text is an original rewrite, never copied.
- Carousels have 1-10 images.
- Analytics runs only on posts at least 48 hours old; a refused or missing metric is marked unavailable, never a crash.
- Score = `(shares + saves) / reach`, with likes and comments weighted lower (weights in `config.py`).
- Selection is a bandit: 80% best-scoring option, 20% exploration; fewer than 30 scored posts means even exploration and no declared winner.
- The learning loop changes only topic, format, time slot and headline style, never safety rules, source-credit rules or `MODE`.
- A publish is never retried blindly; the post is recorded as `publishing` before the API call so a crash cannot cause a duplicate.
- Daily post cap defaults to 5, far below Meta's documented limits (25 or 100 per 24h; sources disagree, re-check before launch).
- Secrets come from environment variables only, never from files in the repo.
- Cron schedules are written in UTC; user-facing times are Asia/Kolkata.

## Review Focus

- Telugu conjuncts and vowel signs render as broken glyphs if raqm shaping is missing; expected: render fails loudly instead of producing a bad image.
- A very long Telugu headline overflows the card; expected: text wraps and shrinks to fit, and absurdly long input is truncated at a word boundary.
- A story with no summary, no image, or an English-only title; expected: skipped or flagged, never posted blank.
- A crash between the Instagram publish call and the database write; expected: the next run sees `publishing`, checks Instagram for the media, and does not repost.
- Instagram returns a refused metric or 48h-old posts have zero reach; expected: metric marked unavailable and no division by zero in the score.

---

### Task 1: Scaffold, config, models, database

**Files:**
- Create: `pyproject.toml`, `src/tgnews/__init__.py`, `src/tgnews/config.py`, `src/tgnews/models.py`, `src/tgnews/db.py`
- Test: `tests/test_config.py`, `tests/test_db.py`

**Interfaces:**
- Produces: `Settings` (frozen dataclass; `Settings.from_env(env: Mapping[str,str]) -> Settings`; fields `mode`, `daily_cap`, `database_url`, `score_weights`, `explore_rate=0.2`, `min_scored_posts=30`). `Story(id, title, summary, url, source_name, published_at, topic_hint)`. `PostState` enum: `draft, approved, publishing, published, failed, expired, rejected`. `db.init(engine)`, `db.save_story`, `db.story_seen(url) -> bool`, `db.save_post`, `db.update_post_state`, `db.posts_due_for_analysis(now) -> list[Post]`.

- [ ] **Step 1: Test `test_mode_defaults_to_review` and `test_invalid_mode_raises` (mode "turbo" raises `ValueError`).**
- [ ] **Step 2: Test `test_story_seen_after_save` and `test_post_state_roundtrip` on in-memory SQLite.**
- [ ] **Step 3: Run tests, confirm they fail.** `pytest tests -v`
- [ ] **Step 4: Implement the modules to the signatures above; tables: `stories`, `posts` (story_id, format, topic, time_slot, headline_style, media_id, state, posted_at, metrics JSON), `drafts`, `learning`.**
- [ ] **Step 5: Run tests, confirm pass; commit** `feat: scaffold, config, models, db`

### Task 2: Fetch Telangana stories

**Files:** Create `src/tgnews/fetch.py`; Test `tests/test_fetch.py`, fixtures `tests/fixtures/*.xml`

**Interfaces:**
- Consumes: `Story`.
- Produces: `class NewsSource(Protocol): def fetch(self) -> list[Story]`; `RssSource(url, name)`; `fetch_stories(sources, keywords=TELANGANA_KEYWORDS) -> list[Story]` (isolates one failing source and continues).

- [ ] **Step 1: Tests with recorded RSS fixtures:** `test_rss_parses_stories`, `test_non_telangana_filtered_out` (a Mumbai story is dropped, a Hyderabad one kept), `test_one_failing_source_does_not_stop_others`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement with feedparser; keyword list includes Telangana, Hyderabad, Warangal, Karimnagar, Khammam, Nizamabad and Telugu spellings.**
- [ ] **Step 4: Run, confirm pass; commit** `feat: rss fetch with telangana filter`

*Open item for review:* the news API provider is not chosen yet. After you pick one and its terms allow republishing summaries, add `NewsApiSource` in this task's file following the same `NewsSource` interface with a recorded-response fixture test.

### Task 3: Dedupe and safety filter

**Files:** Create `src/tgnews/dedupe.py`, `src/tgnews/safety.py`, `src/tgnews/blocklist.yaml`; Test `tests/test_dedupe.py`, `tests/test_safety.py`

**Interfaces:**
- Produces: `dedupe(stories, seen_urls, recent_titles, threshold=0.85) -> list[Story]`; `class Verdict(Enum): ALLOW, HOLD, BLOCK`; `check(story, mode) -> Verdict`.

- [ ] **Step 1: Tests:** `test_same_url_dropped`, `test_near_identical_title_dropped`, `test_distinct_story_kept`, `test_blocked_topic_blocks` (e.g. a story matching a communal-violence keyword), `test_auto_mode_holds_borderline_story_for_review`, `test_review_mode_allows_borderline_story`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement title similarity with `difflib.SequenceMatcher`; blocklist is a YAML file of keyword groups with a verdict each (you will review and edit it).**
- [ ] **Step 4: Run, confirm pass; commit** `feat: dedupe and safety filter`

### Task 4: Rewrite with Claude and validate

**Files:** Create `src/tgnews/rewrite.py`; Test `tests/test_rewrite.py`

**Interfaces:**
- Consumes: `Story`.
- Produces: `RewrittenPost(headline_te, points_te: list[str], caption_te, hashtags: list[str], source_credit, topic, headline_style)`; `rewrite(story, client, style_hint) -> RewrittenPost`; `validate(post) -> list[str]` (empty means valid).

- [ ] **Step 1: Tests with a fake client:** `test_valid_telugu_post_passes`, `test_english_only_headline_fails`, `test_missing_source_credit_fails`, `test_overlong_headline_fails` (limit 90 chars), `test_rewrite_retries_once_then_raises_RewriteFailed`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement with the anthropic SDK requesting structured JSON; Telugu check is the share of characters in the Telugu Unicode block (U+0C00-U+0C7F) above 0.5 in headline and caption.**
- [ ] **Step 4: Run, confirm pass; commit** `feat: claude rewrite with validation`

### Task 5: Render Telugu cards and carousels

**Files:** Create `src/tgnews/render.py`, `assets/fonts/NotoSansTelugu-*.ttf` (OFL), `assets/templates/`; Test `tests/test_render.py`

**Interfaces:**
- Consumes: `RewrittenPost`.
- Produces: `render_card(post, out_dir) -> list[Path]` (one 1080x1350 JPEG); `render_carousel(post, out_dir) -> list[Path]` (3-5 JPEGs, headline slide, key-point slides, source slide); `assert_shaping_available()` raises if raqm is missing.

- [ ] **Step 1: Tests:** `test_card_is_1080x1350_jpeg`, `test_carousel_slide_count_between_3_and_5`, `test_long_headline_fits_inside_margins` (render a 90-char headline and assert no pixel outside the margin box), `test_missing_raqm_raises`.
- [ ] **Step 2: Run, confirm fail.**
- [ ] **Step 3: Implement with Pillow `ImageFont.truetype(..., layout_engine=ImageFont.Layout.RAQM)`; shrink font until text fits the box.**
- [ ] **Step 4: Run, confirm pass; visually open one output and check conjuncts such as క్ష and స్త render correctly. Commit** `feat: telugu card and carousel rendering`

### Task 6: Public image storage

**Files:** Create `src/tgnews/storage.py`; Test `tests/test_storage.py`

**Interfaces:** Produces `upload(paths: list[Path], prefix: str) -> list[str]` (public HTTPS URLs) over an S3-compatible bucket configured by env (`STORAGE_ENDPOINT`, `STORAGE_BUCKET`, `STORAGE_PUBLIC_BASE`).

- [ ] **Step 1: Test with a stubbed S3 client:** `test_upload_returns_public_urls_in_order`, `test_upload_failure_raises_StorageError`.
- [ ] **Step 2: Run fail, implement with boto3, run pass; commit** `feat: public image storage`

### Task 7: Publish via Instagram Graph API

**Files:** Create `src/tgnews/publish.py`; Test `tests/test_publish.py`, fixtures `tests/fixtures/ig_*.json`

**Interfaces:**
- Consumes: image URLs, `RewrittenPost`, `db` functions.
- Produces: `publish_post(post_id, urls, caption, client, db) -> str` (media id); `refresh_token(client) -> Token` and `token_days_left(token) -> int`.

- [ ] **Step 1: Tests with recorded responses:** `test_single_image_creates_container_then_publishes`, `test_carousel_creates_children_then_parent`, `test_container_not_ready_waits_then_publishes`, `test_state_is_publishing_before_api_call` (assert state recorded first), `test_api_error_marks_failed_and_does_not_retry`, `test_existing_publishing_post_checks_instagram_before_reposting`.
- [ ] **Step 2: Run fail; implement with httpx against the Graph API (create container, poll status, publish); run pass; commit** `feat: instagram publishing with duplicate guard`

### Task 8: Review gate

**Files:** Create `src/tgnews/gate.py`; Test `tests/test_gate.py`

**Interfaces:** Produces `send_draft(post_id, previews, caption) -> None` (Telegram message with Approve, Reject, Edit-caption); `poll_decisions(now) -> list[Decision]`; `expire_old_drafts(now, max_age_hours=6) -> int`.

- [ ] **Step 1: Tests with a fake Telegram API:** `test_approve_sets_state_approved`, `test_reject_sets_state_rejected`, `test_edited_caption_is_saved_and_revalidated`, `test_old_draft_expires`, `test_only_configured_chat_id_can_approve` (messages from other chats are ignored).
- [ ] **Step 2: Run fail; implement; run pass; commit** `feat: telegram review gate`

### Task 9: Analyze insights

**Files:** Create `src/tgnews/analyze.py`; Test `tests/test_analyze.py`

**Interfaces:** Produces `pull_insights(media_id, client) -> Metrics` (dict of metric to value or `None` for unavailable); `score(metrics, weights) -> float | None`; `run_analysis(now, client, db) -> int`.

- [ ] **Step 1: Tests:** `test_score_formula_matches_spec` (shares 3, saves 2, reach 100 gives the expected value with likes and comments at their configured weights), `test_zero_reach_returns_none`, `test_refused_metric_marked_unavailable_not_raised`, `test_posts_under_48h_are_skipped`.
- [ ] **Step 2: Run fail; implement; run pass; commit** `feat: insights analysis and scoring`

### Task 10: Learning loop and weekly report

**Files:** Create `src/tgnews/learn.py`, `src/tgnews/report.py`; Test `tests/test_learn.py`, `tests/test_report.py`

**Interfaces:** Produces `choose(attribute, options, db, rng) -> str`; `update_learning(db) -> None`; `weekly_report(db, client) -> str`.

- [ ] **Step 1: Tests with a seeded RNG:** `test_under_30_scored_posts_explores_evenly` (counts across 1000 draws within tolerance), `test_over_30_picks_best_about_80_percent`, `test_attribute_options_are_fixed` (loop cannot return an unknown format or touch safety settings), `test_report_says_insufficient_data_under_30_posts`.
- [ ] **Step 2: Run fail; implement bandit from mean score per option; report text comes from computed numbers only; run pass; commit** `feat: learning loop and weekly report`

### Task 11: Pipeline and CLI

**Files:** Create `src/tgnews/pipeline.py`, `src/tgnews/cli.py`; Test `tests/test_pipeline.py`

**Interfaces:**
- Consumes: Tasks 1-10.
- Produces: `run_once(settings, deps) -> RunSummary`; CLI commands `tgnews run`, `tgnews dry-run --out DIR`, `tgnews publish-approved`, `tgnews analyze`, `tgnews report`, `tgnews refresh-token`.

- [ ] **Step 1: Tests with all dependencies faked:** `test_review_mode_creates_draft_and_does_not_publish`, `test_auto_mode_publishes_allowed_story`, `test_auto_mode_holds_flagged_story_as_draft`, `test_daily_cap_stops_run`, `test_dry_run_writes_files_and_makes_no_instagram_calls`, `test_one_story_failure_does_not_abort_run`.
- [ ] **Step 2: Run fail; implement orchestration in the stage order from the spec; run pass; commit** `feat: pipeline and cli`

### Task 12: Scheduled workflows and setup guide

**Files:** Create `.github/workflows/post.yml`, `publish-approved.yml`, `analyze.yml`, `report.yml`, `refresh-token.yml`, `README.md`, `.env.example`

- [ ] **Step 1: Workflows call the CLI commands from Task 11 on UTC cron schedules matching your chosen IST posting windows; all secrets from repository secrets.**
- [ ] **Step 2: README covers, in order:** switch the old account to Business/Creator and link a Facebook Page; create the Meta developer app and token; create the Telegram bot; create image storage; set secrets; run `tgnews dry-run` and check the Telugu images; run once in review mode on a throwaway test account; then switch to your real page.
- [ ] **Step 3: Run the whole suite and a final dry-run; commit** `docs: workflows and setup guide`
