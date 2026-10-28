# tgnews: Telangana news to Telugu Instagram posts

A scheduled job that fetches Telangana news from RSS feeds, rewrites each story as an original Telugu post with source credit (Claude), renders it as an image or carousel (Pillow), and publishes it through the official Instagram API. In `review` mode every draft goes to a private Telegram chat for Approve, Reject or Edit caption first. A daily job reads post insights and a learning loop adjusts topic, format, time slot and headline style.

Design: `docs/superpowers/specs/2026-10-28-telangana-instagram-bot-design.md` (if present in your checkout).

**Verification note.** Statements marked *(unverified)* were not checked against an official source while writing this guide. Meta and GitHub change their dashboards, limits and prices often. Where a menu name or a number matters, check the current official docs rather than trusting this file.

## Setup, in order

Do these in order. Do not skip the throwaway-account test (step 9).

### 1. Switch the Instagram account to Business or Creator and link a Facebook Page

The account that will post must be a professional (Business or Creator) account linked to a Facebook Page. Find the switch in the Instagram app settings and the Page linking in Meta's current help pages; menu names change, so check Meta's current docs.

### 2. Create the Meta developer app and the token

Use the "Instagram API with Instagram Login" product in a Meta developer app. Request these permissions:

- `instagram_business_basic`
- `instagram_business_content_publish`
- `instagram_business_manage_insights`

Generate a long-lived Instagram user token for the posting account. You need three values:

- `IG_ACCESS_TOKEN`: the long-lived token.
- `IG_USER_ID`: the Instagram account ID.
- `IG_API_VERSION`: the Graph API version shown in the Meta dashboard, written like `v23.0` (that example is only a format). There is no default; the program refuses to start without it.

Notes:

- The token refresh endpoint needs a token that is at least 24 hours old and not yet expired, and a refreshed token is valid for 60 days (verified in Meta's refresh_access_token reference). `refresh-token.yml` refreshes weekly and stores the newest token in the database, which is then used in preference to `IG_ACCESS_TOKEN`.
- Reading insights for accounts beyond your own may need Meta App Review. The timeline is *(unverified)*; one source said 2 to 4 weeks. Start in development mode on your own account.
- Instagram's publishing limit per 24 hours *(unverified)*: sources disagreed (25 vs 100). The bot caps itself far lower (`DAILY_CAP`, default 5). Re-check Meta's docs before launch.
- AI disclosure is your decision. Meta has an `is_ai_generated` publish parameter mentioned in a changelog; this project does not use it. Decide according to Meta's current policy and your own judgement, and add it to the code or caption yourself if you choose.

### 3. Create the Telegram bot

1. In Telegram, talk to BotFather, create a bot, and copy the token (`TELEGRAM_BOT_TOKEN`).
2. Open a chat with your new bot and send it a message, then find the chat ID (`TELEGRAM_CHAT_ID`) with Telegram's `getUpdates` method or any method you trust.
3. Use a **private chat** only. Anyone in the configured chat can approve posts. Updates from other chats are ignored.
4. Do not set a webhook on this bot; the program reads decisions by polling `getUpdates` *(from general Telegram API knowledge, unverified here)*.

### 4. Create image storage

Instagram needs a public HTTPS URL for each image. Create an S3-compatible bucket (any provider) with:

- public read access for the image prefix (the program uploads JPEGs only and the URLs must be reachable without credentials),
- a public base URL that serves the bucket (`STORAGE_PUBLIC_BASE`, must start with `https://`),
- an access key with write access.

Values: `STORAGE_ENDPOINT`, `STORAGE_ACCESS_KEY_ID`, `STORAGE_SECRET_ACCESS_KEY`, `STORAGE_BUCKET`, `STORAGE_PUBLIC_BASE`, optional `STORAGE_REGION` (default `auto`).

### 5. Create a hosted Postgres database

Runners have no persistent disk, so `DATABASE_URL` must be a hosted Postgres URL, for example `postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME`. The workflows install the driver with `pip install -e .[postgres]` (extra `postgres = ["psycopg[binary]"]`). Do not use sqlite in Actions; it would be empty on every run and could double-post.

### 6. Add sources: `config/sources.yaml`

The repo ships this file **empty** (`feeds: []`). You must add verified Telangana RSS feeds:

```yaml
feeds:
  - name: Example Telangana News
    url: https://news.example.com/telangana/rss.xml
```

Only add feeds whose terms allow republishing summaries; check each publisher's terms. Only RSS is implemented. A news API provider has **not** been chosen; if you add one, check that its terms allow republishing summaries.

### 7. Review the safety blocklist: `src/tgnews/blocklist.yaml`

This is a draft. The Telugu terms have **not** been verified by a fluent Telugu reader. Have one review and edit them before relying on the filter. `block` groups are never posted in any mode; `hold` groups are allowed in review mode and kept as drafts in auto mode. A broken file makes the filter raise an error rather than allow everything.

### 8. Set secrets and variables

In the GitHub repository settings (check GitHub's current docs for where Actions secrets and variables live), add.

Repository **secrets**:

`ANTHROPIC_API_KEY`, `DATABASE_URL`, `IG_ACCESS_TOKEN`, `IG_USER_ID`, `IG_API_VERSION`, `STORAGE_ENDPOINT`, `STORAGE_ACCESS_KEY_ID`, `STORAGE_SECRET_ACCESS_KEY`, `STORAGE_BUCKET`, `STORAGE_PUBLIC_BASE`, `STORAGE_REGION`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

(`STORAGE_REGION` may be an empty secret; the default is `auto`.)

Repository **variables** (not secret): `MODE` (defaults to `review` if unset), `DAILY_CAP`, `BRAND_NAME`, `TIME_SLOTS`, `ACTIVE_SLOT_COUNT`. Unset variables fall back to the defaults in `src/tgnews/config.py`. See `.env.example` for every name with a one-line meaning. Secrets are never printed; error output has them scrubbed.

If you change `TIME_SLOTS`, also edit the cron times in `.github/workflows/post.yml`, which must match the slots in UTC (a test checks the defaults).

### 9. Dry-run, then a throwaway account

Locally, with `ANTHROPIC_API_KEY` set and real feeds in `config/sources.yaml`:

```
pip install -e ".[dev]"
tgnews dry-run --out ./out
```

This renders images and captions into `./out` with no Instagram, Telegram or storage calls. Open the images and **check the Telugu carefully**: letters must be properly joined (the program refuses to start if Pillow lacks raqm; on Linux install `libraqm0 libfribidi0 libharfbuzz0b`). Have a Telugu reader check the text.

Then point all secrets at a **throwaway Instagram test account** (and a test bucket/database), keep `MODE=review`, and trigger **tgnews post** once with `workflow_dispatch`. Approve one draft in Telegram, trigger **tgnews publish approved**, and confirm the post appears.

### 10. Switch to the real page

Replace `IG_ACCESS_TOKEN`, `IG_USER_ID`, and the database with the real ones (use a fresh database so test history does not leak into learning), set `BRAND_NAME` to the page's name, and keep `MODE=review` for the first weeks. Only move to `auto` after you trust the blocklist and the output; auto mode can publish a wrong or sensitive story.

## Workflows

All five support manual runs (`workflow_dispatch`) and share one `concurrency` group named `tgnews` so runs never overlap (the guard against double-posting). Cron times are UTC with IST in the comments.

| File | When (UTC / IST) | What it does |
|---|---|---|
| `post.yml` | 02:00, 07:00, 13:00, 15:30 / 07:30, 12:30, 18:30, 21:00 | `tgnews run` (fetch, dedupe, safety, rewrite, render, upload, then draft to Telegram in review mode or publish in auto mode), then `tgnews publish-approved`. These match the default `TIME_SLOTS`. Each run takes the nearest slot; only the day's active slots post. |
| `publish-approved.yml` | every 30 min, 01:00 to 17:30 / 06:30 to 23:00 | `tgnews publish-approved`: expires old drafts (6 hours), reads Telegram taps, publishes approved posts. Approvals therefore publish with up to about 30 minutes of delay, longer if GitHub delays the run. |
| `analyze.yml` | daily 20:20 / 01:50 | `tgnews analyze`: pulls insights for posts at least 48 hours old and updates the learning table. |
| `report.yml` | Mondays 03:10 / 08:40 | `tgnews report`: writes the weekly report and sends it to Telegram. |
| `refresh-token.yml` | Sundays 04:40 / 10:10 | `tgnews refresh-token`: refreshes the Instagram token and stores it. |

Cost and scheduling caveats:

- Actions minutes included depend on your plan and on whether the repository is public or private. Check GitHub's current pricing and limits before enabling the schedules. The publish-approved cadence alone is 34 runs a day, each installing dependencies. This count is arithmetic; the cost is *(unverified)*.
- GitHub's docs say scheduled workflows may be delayed during periods of high load (verified); the typical size of delays (a third-party source says 5 to 30 minutes at the top of the hour) is *(unverified against GitHub's own docs)*. Posts and approvals can therefore be late.
- GitHub's docs say that in a **public** repository scheduled workflows are disabled automatically when there has been no repository activity for 60 days (verified). Whether private repositories are affected was not verified. If posts stop, check the Actions tab and re-enable the workflows.
- Behaviour of GitHub concurrency when several runs queue in one group (it keeps one pending run and may cancel older pending ones) is *(unverified)*. Treat a skipped scheduled run as possible.

## How learning works

Each post is tagged with topic, format (card or carousel), time slot and headline style. Insights give reach, likes, comments, shares and saves; the score is roughly `(shares + saves) / reach` with likes and comments weighted lower. Selection is a bandit: about 80% of the time the best-scoring option, about 20% exploring. Until **30 scored posts** exist, selection is evenly exploratory and no winner is declared. The loop never changes safety rules, source credit or `MODE`.

## Reading the weekly report

The report (sent to Telegram on Mondays) is worded by Claude from numbers computed in code. Read it as a statement of what the data does and does not support. With fewer than 30 scored posts it should say no winner can be declared. Treat small differences between options with few posts as noise; the report does not explain causes and neither should you.

## Known limitations

- Only RSS sources are implemented. No news API is wired in.
- A story is marked seen as soon as it is chosen and cleared by safety. If rewriting, rendering or publishing then fails, that story is not retried.
- Runs must not overlap. The shared concurrency group guards this in Actions; do not also run the CLI elsewhere against the same database and account.
- The BLOCK/HOLD safety check is re-run on each run over the story text; a story blocked once is not remembered as blocked, it is simply checked again.
- A failed publish is never retried blindly (to avoid duplicates); it is reconciled against Instagram's recent media on the next publish run.
- The Telugu blocklist and the Telugu output are unreviewed by a native speaker until you do that review.
- No video, Stories or comment replies.

## Development

```
pip install -e ".[dev]"
pytest
tgnews --help
```
