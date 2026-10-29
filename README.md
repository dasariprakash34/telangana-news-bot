# tgnews: Telangana news to Telugu Instagram posts

A scheduled job that fetches Telangana news from RSS feeds, rewrites each story as an original Telugu post with source credit (an LLM; free Google Gemini tier by default), renders it as an image or carousel (Pillow), and publishes it through the official Instagram API. In `review` mode every draft goes to a private Telegram chat for Approve, Reject or Edit first: reply with a new caption, or with `headline: ...` plus `- point` lines to fix the poster text (the images are redrawn and a fresh preview with buttons is sent). A daily job reads post insights and a learning loop adjusts topic, format, time slot and headline style.

Design: `docs/superpowers/specs/2026-10-28-telangana-instagram-bot-design.md` (if present in your checkout).

**Verification note.** Statements marked *(unverified)* were not checked against an official source while writing this guide. Meta and GitHub change their dashboards, limits and prices often. Where a menu name or a number matters, check the current official docs rather than trusting this file.

## Before you start

- **Gemini API key** (`GEMINI_API_KEY`) and **model id** (`LLM_MODEL`): the default LLM provider is the Google Gemini API free tier, which costs nothing and needs no credit card *(per a third-party page dated Oct 2026; unverified)*. Create a key in Google AI Studio (check Google AI Studio's current UI for where API keys live). Then copy a current Flash or Flash-Lite model id from AI Studio into `LLM_MODEL`; the program has no default for it on purpose, because model ids change. Free-tier quotas are not a fixed public table and may change, so check the limits shown in AI Studio. Prefer a Flash-Lite or other non-thinking model: thinking-capable models spend the output token budget on internal thinking, and Telugu text is token-expensive, so replies can be cut off or empty (the program always requests at least 8192 output tokens as a margin). On the free tier Google may use your prompts to improve its models; that is acceptable here because the inputs are public news text, but do not send anything private.
- **Anthropic (optional, pay-as-you-go)**: set the repository variable `LLM_PROVIDER=anthropic`, add the secret `ANTHROPIC_API_KEY` (from the Anthropic console) and install the extra with `pip install -e ".[anthropic]"`, which means changing the `Install tgnews` step in the workflows to `-e ".[postgres,anthropic]"`. `LLM_MODEL` is then optional. This provider is billed per use; the default stack is not.
- **Meta development mode**: while your Meta app is in development mode, the Instagram account must be added to the app with a tester or other role. Check Meta's current docs for the exact steps.
- **`IG_USER_ID`**: the Instagram user ID of the account the token belongs to. Meta's Business Login docs say the token exchange response includes a `user_id` field (verified). I could not verify a `GET /me?fields=user_id,username` call, so check Meta's current docs for how to fetch it with your token. Use the same ID the API returns for that token's account.
- **Throwaway test account** (step 9): it needs its own Business/Creator switch, Facebook Page link, token and user ID, and must also be added to the Meta app.
- **GitHub**: push this repo to GitHub with the workflows on the **default branch**. Scheduled runs only run from the default branch; for manual runs (`workflow_dispatch`) the workflow file must also exist on the default branch for the Run button to appear *(the default-branch rule is from general GitHub knowledge, not re-verified here)*. On a new or forked repository Actions may need to be enabled first (check GitHub's current docs).

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
- Instagram's publishing limit: Meta's content-publishing guide says 100 API-published posts per 24-hour moving period (a carousel counts as one); the same page's carousel section says 50, so treat 50 as the safe figure. The bot caps itself far lower (`DAILY_CAP`, default 5).
- AI disclosure is your decision. Meta has an `is_ai_generated` publish parameter mentioned in a changelog; this project does not use it. Decide according to Meta's current policy and your own judgement, and add it to the code or caption yourself if you choose.

### 3. Create the Telegram bot

1. In Telegram, talk to BotFather, create a bot, and copy the token (`TELEGRAM_BOT_TOKEN`).
2. Open a chat with your new bot and send it a message, then find the chat ID (`TELEGRAM_CHAT_ID`) with Telegram's `getUpdates` method or any method you trust.
3. Use a **private chat** only. Anyone in the configured chat can approve posts. Updates from other chats are ignored.
4. Do not set a webhook on this bot; the program reads decisions by polling `getUpdates` *(from general Telegram API knowledge, unverified here)*.

### 4. Create image storage (free: a public GitHub repo)

Instagram needs a public HTTPS URL for each image. The default backend (`STORAGE_PROVIDER=github`) costs nothing: the program commits each JPEG into a **separate public GitHub repo** (the "images repo") through GitHub's Contents API and serves it from a public URL.

1. Create a new **public** repository for images only (for example `your-user/tgnews-images`) with a `main` branch (initialise it with a README so the branch exists). It must be public: Instagram's servers fetch the images without credentials.
2. Create a **fine-grained personal access token** limited to **that one repository** with **Contents: read and write**. Check GitHub's current UI for the exact menu path. Set an expiry you will remember to renew.
3. Repository secret `IMAGES_GH_TOKEN` (the name must not start with `GITHUB_`; GitHub forbids Actions secret names that do) and repository variable `IMAGES_GH_REPO` (`owner/name`). Optional variables: `IMAGES_GH_BRANCH` (default `main`), `IMAGES_PUBLIC_BASE`, `IMAGES_KEEP_DAYS`, `STORAGE_PROVIDER` (`github` if unset).

Images are committed as `posts/YYYYMMDD/<story>-<random>-<n>.jpg`. The path is unique per post; if GitHub reports that a file already exists the upload fails rather than overwriting it. Uploads are sequential with a one-second pause, to stay clear of GitHub's secondary rate limits.

**Which URL Instagram fetches.** By default `https://raw.githubusercontent.com/OWNER/REPO/BRANCH/posts/...`. If Instagram rejects that host, set `IMAGES_PUBLIC_BASE` to a CDN style base such as `https://cdn.jsdelivr.net/gh/OWNER/REPO@BRANCH` (https only). **Instagram accepting either URL form is UNVERIFIED. Test with the throwaway account first.** You can check that a URL is publicly fetchable (HTTP 200 and `image/jpeg`) with `tgnews check-images https://...`; it uploads nothing. That proves the URL works for you, not that Instagram's fetcher accepts it; only a real test post shows that. CDNs may also cache a path for a while, which is harmless here because paths are never reused.

**Pruning.** `analyze.yml` also runs `tgnews prune-images` daily. It deletes files in `posts/YYYYMMDD/` directories older than `IMAGES_KEEP_DAYS` days (default 14, minimum 3 enforced in code; anything outside `posts/` is untouched). Each run makes at most 60 delete attempts, so a large backlog is cleared over several days; the output shows `deleted=` and `skipped=`. Images older than `IMAGES_KEEP_DAYS` are deleted by date only, without looking at post state. This is safe because `tgnews publish-approved` never publishes a post older than 48 hours (older approved posts are marked expired), drafts expire after 5 hours and stories older than 48 hours are skipped, so the default 14 days leaves a wide margin.

**Pruning does not shrink the repo.** Deleting a file only removes it from the current branch; git history keeps every blob, so the repo keeps growing. Rough estimate from the defaults: 5 posts per day (`DAILY_CAP`) x about 3 slides x about 200 KB per JPEG is roughly 3 MB per day, about 1 GB per year. This is an estimate; the JPEG size is a guess, so look at real sizes after a week. GitHub recommends keeping repositories small: check GitHub's current guidance for the limits. Fallback: every so often (for example once a year, or whenever the repo gets large) create a fresh images repo, point `IMAGES_GH_REPO` (and a new token) at it and delete the old one. Old posts are not needed any more once they are older than 48 hours.

**Caveats (all UNVERIFIED, from general knowledge; test before relying on them).** jsDelivr caches branch references (`@main`) for a while and may also cache a "not found" answer, so a URL fetched right after the commit could briefly fail; if you see that, prefer `raw.githubusercontent.com`, or a CDN URL pinned to a commit. The Contents API returns a limited number of entries per directory (about 1000 files), which is far above what 14 days of posts need but matters if you raise `IMAGES_KEEP_DAYS` a lot.

**Everything in a public repo is public**: the images, and also this code repo's `blocklist.yaml` and `config/sources.yaml` if you make the code repo public. Never commit secrets. A public *code* repo is also what makes Actions free: GitHub's docs say standard runners are free for public repositories (check the current docs; private repos have a monthly minutes quota). Review the blocklist and sources with that in mind before publishing the repo.

**Optional advanced backend: S3-compatible bucket** (`STORAGE_PROVIDER=s3`; some providers need a payment method). Create a bucket with public read access for the image prefix, a public base URL that serves it (`STORAGE_PUBLIC_BASE`, must start with `https://`) and an access key with write access. Secrets: `STORAGE_ENDPOINT`, `STORAGE_ACCESS_KEY_ID`, `STORAGE_SECRET_ACCESS_KEY`, `STORAGE_BUCKET`, `STORAGE_PUBLIC_BASE`, optional `STORAGE_REGION` (default `auto`). These are ignored with the github provider. S3 images are not pruned by this program.

### 5. Create a hosted Postgres database

Runners have no persistent disk, so `DATABASE_URL` must be a hosted Postgres URL, for example `postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME`. If your provider gives you `postgres://...` or `postgresql://...`, the program rewrites it to `postgresql+psycopg://...` itself; other schemes (sqlite) are left alone. The workflows install the driver with `pip install -e .[postgres]` (extra `postgres = ["psycopg[binary]"]`). Do not use sqlite in Actions; it would be empty on every run and could double-post.

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

`GEMINI_API_KEY`, `ANTHROPIC_API_KEY` (only if you use the anthropic provider; may be left unset or empty), `DATABASE_URL`, `IG_ACCESS_TOKEN`, `IG_USER_ID`, `IG_API_VERSION`, `IMAGES_GH_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

The s3-only secrets (`STORAGE_ENDPOINT`, `STORAGE_ACCESS_KEY_ID`, `STORAGE_SECRET_ACCESS_KEY`, `STORAGE_BUCKET`, `STORAGE_PUBLIC_BASE`, `STORAGE_REGION`) are needed only with `STORAGE_PROVIDER=s3` and may be left unset.

Repository **variables** (not secret): `STORAGE_PROVIDER`, `IMAGES_GH_REPO`, `IMAGES_GH_BRANCH`, `IMAGES_PUBLIC_BASE`, `IMAGES_KEEP_DAYS` (see step 4), `LLM_PROVIDER` (`gemini` if unset, or `anthropic`), `LLM_MODEL` (required for gemini: the model id copied from Google AI Studio), `MODE` (defaults to `review` if unset), `DAILY_CAP`, `BRAND_NAME`, `TIME_SLOTS`, `ACTIVE_SLOT_COUNT`, `IGNORE_SLOTS` (testing only: set to `true` so a manual run ignores the active-slot check; delete it afterwards), `REQUIRE_SOURCE_CREDIT` (`true` by default; `false` drops the source line from captions and from the edit check), `PER_RUN_CAP` (most posts or drafts one run may create; default `ceil(DAILY_CAP / ACTIVE_SLOT_COUNT)`, so a single run cannot burst the whole day's allowance). Unset variables fall back to the defaults in `src/tgnews/config.py`. See `.env.example` for every name with a one-line meaning. Secrets are never printed: error messages shown or sent to Telegram are limited to this project's own sanitized messages (anything else is reduced to its error type), and any secret value found in them is replaced with `[REDACTED]`. An invalid `DATABASE_URL` is reported only as "invalid or unreachable".

If you change `TIME_SLOTS`, also edit the cron times in `.github/workflows/post.yml`, which must match the slots in UTC (a test checks the defaults).

### 9. Dry-run, then a throwaway account

Locally, with `GEMINI_API_KEY` and `LLM_MODEL` set (or the anthropic variables) and real feeds in `config/sources.yaml`:

```
pip install -e ".[dev]"
tgnews dry-run --out ./out
```

This renders images and captions into `./out` with no Instagram, Telegram or storage calls. Open the images and **check the Telugu carefully**: letters must be properly joined (`run` and `dry-run` check this first and stop with a `ShapingUnavailable` error if Pillow lacks raqm; on Linux install `libraqm0 libfribidi0 libharfbuzz0b`). Have a Telugu reader check the text.

Then point all secrets at a **throwaway Instagram test account** (and a test images repo/database), keep `MODE=review`, and trigger **tgnews post** once with `workflow_dispatch`. Approve one draft in Telegram, trigger **tgnews publish approved**, and confirm the post appears.

### 10. Switch to the real page

Replace `IG_ACCESS_TOKEN`, `IG_USER_ID`, and the database with the real ones (use a fresh database so test history does not leak into learning), set `BRAND_NAME` to the page's name, and keep `MODE=review` for the first weeks. Only move to `auto` after you trust the blocklist and the output; auto mode can publish a wrong or sensitive story.

## Workflows

All five support manual runs (`workflow_dispatch`) and share one `concurrency` group named `tgnews` so runs never overlap (the guard against double-posting). Cron times are UTC with IST in the comments.

| File | When (UTC / IST) | What it does |
|---|---|---|
| `post.yml` | every 30 minutes, 00:30-18:00 UTC / 06:00-23:30 IST (36 slots) | `tgnews run` (fetch, dedupe, safety, rewrite, render, upload, then draft to Telegram in review mode or publish in auto mode), then `tgnews publish-approved`. These match the default `TIME_SLOTS`. Each run takes the nearest slot; only the day's active slots post. |
| `publish-approved.yml` | every 30 min at :15 and :45, 01:15 to 17:45 / 06:45 to 23:15 | `tgnews publish-approved`: reads Telegram taps, then expires untouched drafts (older than 5 hours), then publishes approved posts. Approval behaviour: a tap is read when the next publish run polls Telegram, **before** old drafts are expired, so a tap on a still-pending draft is honoured even if the draft is older than 5 hours. Expiry only removes drafts nobody acted on. Latency: inside the window an approval publishes within about 30 minutes (longer if GitHub delays the run); an evening approval (after 17:45 UTC / 23:15 IST) publishes at the next run, 01:15 UTC (06:45 IST) at the latest. `post.yml` also runs `publish-approved` after each posting run. |
| `telegram-listener.yml` | continuous: one run lasts about 5.6 hours, then starts the next; plus a backup schedule every 6 hours | `tgnews listen`: holds a Telegram long poll open, so an Approve, Reject or caption edit is handled within seconds and an approved post is published right away, with a confirmation message in Telegram. The database is queried only when a tap arrives. It uses its own concurrency group so it never blocks posting runs; a post is claimed atomically before publishing, so a listener and a posting run cannot publish the same post twice. If a posting run polls at the same moment, Telegram may answer the listener with a 409; it just polls again. Start the first run by hand (Actions, tgnews telegram listener, Run workflow). |
| `analyze.yml` | daily 20:20 / 01:50 | `tgnews analyze`: pulls insights for posts at least 48 hours old and updates the learning table. Then `tgnews prune-images`: deletes old images from the GitHub images repo (see step 4). |
| `report.yml` | Mondays 03:10 / 08:40 | `tgnews report`: writes the weekly report and sends it to Telegram. |
| `refresh-token.yml` | Sundays 04:40 / 10:10 | `tgnews refresh-token`: refreshes the Instagram token and stores it. |

Cost and scheduling caveats:

- Actions minutes included depend on your plan and on whether the repository is public or private. Check GitHub's current pricing and limits before enabling the schedules. The publish-approved cadence alone is 34 runs a day, each installing dependencies. This count is arithmetic; the cost is *(unverified)*.
- GitHub's docs say scheduled workflows may be delayed during periods of high load (verified); the typical size of delays (a third-party source says 5 to 30 minutes at the top of the hour) is *(unverified against GitHub's own docs)*. Posts and approvals can therefore be late.
- GitHub's docs say that in a **public** repository scheduled workflows are disabled automatically when there has been no repository activity for 60 days (verified). Whether private repositories are affected was not verified. If posts stop, check the Actions tab and re-enable the workflows.
- All workflows share one concurrency group with `cancel-in-progress: false`, so a running job is never cancelled. Only one run per group can be pending, though, so a pending run (for example a `post.yml` slot) can be displaced by a newer one. That means a **missed slot**; this is an accepted risk of the single group. *(The one-pending-run rule is unverified here; check GitHub's concurrency docs.)*

## How learning works

Each post is tagged with topic, format (card or carousel), time slot and headline style. Insights give reach, likes, comments, shares and saves; the score is roughly `(shares + saves) / reach` with likes and comments weighted lower. Selection is a bandit: about 80% of the time the best-scoring option, about 20% exploring. Until **30 scored posts** exist, selection is evenly exploratory and no winner is declared. The loop never changes safety rules, source credit or `MODE`.

## Reading the weekly report

The report (sent to Telegram on Mondays) is worded by the LLM from numbers computed in code. Read it as a statement of what the data does and does not support. With fewer than 30 scored posts it should say no winner can be declared. Treat small differences between options with few posts as noise; the report does not explain causes and neither should you.

## Known limitations

- Only RSS sources are implemented. No news API is wired in.
- A story is claimed (saved as seen) after it is chosen and passes the BLOCK check, and before rewriting. If rewriting or rendering fails, or a post already exists for it, the story is not retried. If storage, Telegram or Instagram fails and no post row was created yet, the claim is released so a later run can retry it. After 2 consecutive such infrastructure failures the run stops with an error and a Telegram alert; an Instagram authentication failure stops it at once.
- If a run on an active slot fetches no stories (no sources configured, all sources failed, or simply nothing found) it exits normally but sends a Telegram alert.
- Operational notes below ("Token reset", "Re-queue a failed post") need direct database access.
- Runs must not overlap. The shared concurrency group guards this in Actions; do not also run the CLI elsewhere against the same database and account.
- A BLOCKed story is skipped before it is saved, so it is not remembered and is simply checked again on every run while it stays in the feed. HOLD stories are saved and become drafts (review mode) or held drafts (auto mode).
- A failed publish is never retried blindly (to avoid duplicates); it is reconciled against Instagram's recent media on the next publish run.
- The Telugu blocklist and the Telugu output are unreviewed by a native speaker until you do that review.
- No video, Stories or comment replies.

## Development

```
pip install -e ".[dev]"
pytest
tgnews --help
```

## Operations

### Token reset

After the first `refresh-token` run the newest Instagram token is stored in the database and **takes priority** over the `IG_ACCESS_TOKEN` secret. If you create a new token and update the `IG_ACCESS_TOKEN` secret, clear the stored one so the new secret is used (and the expiry guard resets):

```sql
DELETE FROM kv WHERE key IN ('ig_access_token', 'ig_token_expires_at');
```

### Re-queue a failed post

A post in state `failed` was not published (a failure before the final publish call). It is not retried automatically. For a post made in review mode (it has a stored draft row with image URLs), you can send it back to the publish queue; the next `publish-approved` run publishes it:

```sql
UPDATE posts SET state = 'approved', container_id = NULL
WHERE id = <POST_ID> AND state = 'failed' AND media_id IS NULL
  AND EXISTS (SELECT 1 FROM drafts WHERE drafts.post_id = posts.id);
```

Check on Instagram first that the post did not go live. This is not offered for posts without a draft row (auto-mode posts, and drafts whose Telegram send failed): they have no stored image URLs, so `publish-approved` would mark them failed again.
