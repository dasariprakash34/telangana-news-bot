# Telangana News Instagram Bot: Design Spec

Date: 2026-10-28
Status: Draft for review

## 1. Goal

An Instagram page that posts Telangana news in Telugu, run as a scheduled cloud job. It fetches stories from news APIs and RSS feeds, rewrites them as original Telugu posts with source credit, publishes them through the official Instagram Graph API, then reads post insights and adjusts what it posts to improve shares, saves and reach.

Success criteria:
- Posts publish on schedule without a laptop being on.
- Every post is an original Telugu rewrite with a visible source credit and passes validation.
- No story is posted twice. No post goes out in auto mode if it trips the safety filter.
- After enough data, the page measurably shifts toward better-performing topics, formats and times, and a weekly report says plainly what is and is not supported by the data.

## 2. Decisions made

| Topic | Decision |
|---|---|
| Language | Telugu (headline image and caption) |
| Approval | `MODE=review` or `MODE=auto`, one config switch |
| Hosting | Scheduled cloud job (GitHub Actions cron or equivalent) |
| Format | Mix of single news card and carousel, chosen by the learning loop |
| Approach | Python pipeline of small modules (not agent-driven, not no-code) |
| Language model use | Claude does rewriting, headline writing and weekly report text only |

## 3. Assumptions to confirm

- "Self-improving" means adjusting topic, format, time slot and headline style from insight data. It does not mean training a model.
- Publishing uses the official Instagram Graph API on a Business or Creator account linked to a Facebook Page.
- Images are generated locally (Pillow with Telugu shaping) and uploaded to public storage, because the API needs a public image URL.

## 4. External constraints (verified from sources found during design)

- Instagram Graph API rate limit is about 200 calls per hour per account.
- Insights are available for reach, likes, comments, shares and saves. Several older metrics were removed (impressions and plays in April 2025, replaced by views). The code must tolerate any metric being refused.
- Insights can lag up to 48 hours, so analytics runs on posts at least 48 hours old.
- Carousels allow 1-10 images.
- Publishing limits: sources disagree (25 feed posts per 24h in one, 100 posts per 24h in another). This design caps posting far below either, and the limit should be re-checked in Meta's documentation before launch.
- Insights access may require Meta App Review for use beyond your own accounts. Timeline is unverified; one source says 2-4 weeks. Plan for development mode on your own account first.

## 5. Architecture

```
schedule trigger
  -> fetch    news APIs + Telangana RSS, filtered to Telangana
  -> dedupe   skip stories already seen or too similar
  -> safety   block-list for sensitive topics; flagged items go to review
  -> rewrite  Claude: Telugu headline, 2-3 line summary, caption, hashtags, source credit
  -> format   card or carousel, chosen by learning loop
  -> render   Pillow image(s), upload to public storage
  -> gate     review: draft sent for approval; auto: continue
  -> publish  create container, check status, publish, save media id
  -> analyze  daily job, posts >= 48h old
  -> learn    update weights for topic, format, time slot, headline style
```

Each stage is a separate module with one purpose and a plain interface (input data in, output data out), so a stage such as the news source or review channel can be replaced without touching the others.

### 5.1 Modules

- `fetch`: calls configured news sources, normalizes to a common story record, filters to Telangana.
- `dedupe`: compares against the post log by URL and by headline similarity.
- `safety`: keyword and category block-list; returns allow, hold-for-review or block.
- `rewrite`: Claude call producing structured output; validator checks Telugu script, length limits, presence of source credit.
- `format`: picks card or carousel using the learning table.
- `render`: draws Telugu text onto branded templates and uploads the files.
- `gate`: in review mode, stores a draft and sends it to a Telegram bot with Approve, Reject, Edit-caption. A separate job every few minutes publishes approved drafts. Drafts expire after a configurable time.
- `publish`: Graph API container creation, status check, publish. A failed publish is never retried blindly, to avoid duplicates.
- `analyze`: pulls insights for posts at least 48 hours old, records metrics, marks unavailable metrics.
- `learn`: scoring and selection described below.
- `report`: weekly summary written by Claude from computed numbers only.

### 5.2 Storage

A small hosted Postgres database (a free-tier managed instance is enough; scheduled runners have no persistent disk) holds: story log, post log (story id, format, topic, time slot, headline style, media id, metrics, state), drafts, and the learning table. Secrets (Meta token, Claude key, news keys, Telegram token) live in the scheduler's secret store.

## 6. Learning loop

- Each post is tagged with topic, format, time slot and headline style.
- Score = `(shares + saves) / reach`, with likes and comments weighted lower.
- Selection is a bandit: about 80% best-scoring option per attribute, about 20% exploration.
- Until about 30 scored posts exist, selection is evenly exploratory and no winner is declared. The weekly report states what the data does and does not support, and does not speculate about causes.
- The loop changes only topic, format, time slot and headline style. It cannot change safety rules, source-credit rules or the mode setting.

## 7. Error handling

- News API failure: retry with backoff, fall back to RSS, then exit quietly and alert.
- Bad or empty rewrite: retry once, then skip the story. Unvalidated text is never posted.
- Publish failure: check container status first, log the failure, keep the post in draft state, never blind-retry.
- Missing or delayed insights: mark unavailable and retry later; the learning loop ignores incomplete posts.
- Token expiry: refresh automatically and alert before expiry; pause posting rather than fail mid-run.
- Daily post cap and duplicate guard apply in both modes.

## 8. Testing

- Unit tests: dedupe, safety filter, score calculation, bandit selection (fixed sample data).
- Dry-run mode: runs through rendering and writes images and captions to a folder with no Instagram calls. Telugu rendering is verified here first.
- Recorded API fixtures for news and Instagram responses so tests run without live keys.
- First live test on a throwaway Instagram test account before the real page.

## 9. Out of scope (v1)

Reels and video, Stories, comment replies, multi-page management.

## 10. Setup prerequisites (to be written as a guide)

- Instagram Business or Creator account linked to a Facebook Page.
- Meta developer app with the needed permissions.
- News API key, Claude API key, Telegram bot token, and public image storage.

## 11. Legal and editorial notes

- Each post is an original rewrite with source credit; text is not copied from articles.
- News API terms of use must be checked for each provider, including whether republishing summaries is permitted. This has not been verified yet.
- Auto mode can publish a wrong or sensitive story; the safety filter reduces but does not remove that risk, and review mode is recommended at launch.
