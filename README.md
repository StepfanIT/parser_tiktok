# TikTok Parser MVP

CLI tool for:
- collecting comments from TikTok videos,
- sending comments from one or many accounts,
- running account health checks,
- working with local Playwright profiles and anti-detect providers.

---

## 1) Project layout

- `main.py` — app entrypoint.
- `app/cli.py` — interactive menu and prompt flow.
- `app/services/comment_service.py` — main business logic for collect/send/check.
- `app/services/send_policy.py` — randomization + scheduling policy.
- `app/integrations/tiktok_client_support/` — browser/session/comment interaction.
- `app/repositories/` — account JSON + CSV load/save.
- `data/accounts/` — account configs and browser profile data.
- `data/comments/outgoing_comments.csv` — outgoing comments input CSV.
- `exports/` — collected comments output CSVs.
- `logs/app.log` — runtime logs.

---

## 2) Setup

```bash
python -m venv venv
venv/Scripts/Activate.ps1
pip install -r requirements.txt
```

Run:

```bash
python main.py
```

---

## 3) Menu navigation

Main menu:
1. Collect comments
2. Send comments
3. Account health check
4. Exit

In most prompts you can type:
- `0`
- `back`
- `menu`
- `exit`

to return to the main menu.

---

## 4) Account config

Each account is a JSON file (example: `data/accounts/<alias>/account.json`).

Important fields:
- `name` — internal alias (used in CSV restrictions).
- `tiktok_username` — optional, recommended for better targeting/filtering.
- `storage_state_path`, `user_data_dir` — session/profile persistence.
- `bootstrap_login_if_missing` — allows manual login fallback.
- `browser_provider` — provider settings (`playwright_local`, `dolphin_anty`, `adspower`).

### Provider secrets
- Dolphin: `api_token` or `api_token_env` (`DOLPHIN_ANTY_TOKEN`).
- AdsPower: `api_key` or `api_key_env` (`ADSPOWER_API_KEY`).

If a secret is not written directly to JSON, env fallback is used.

---

## 5) Collection flow

### Collection modes
1. **All selected accounts on one video**
2. **Each selected account on each listed video**

In multi-video mode you can paste URLs:
- comma-separated,
- line-by-line.

The service merges duplicate comments across passes/videos using `comment_id`.

---

## 6) Sending flow

### Sending modes
1. **Distribute rows across selected accounts**
   - each CSV row is sent once globally (unless auto-switch rule below applies).
2. **Each selected account sends all eligible rows**
   - every selected account sends every eligible row.

### Auto-switch rule (important)
If CSV rows have **no account restrictions** (`account_name`, `allowed_accounts`, `eligible_accounts` are empty),
the app automatically switches to **all-accounts mode**.

### Concurrency
When multiple accounts are eligible, batches are executed concurrently with a thread pool.
If one account batch fails, other account batches continue, and failed rows are returned with `batch_error` status.

---

## 7) Outgoing CSV format

File: `data/comments/outgoing_comments.csv`

Required columns:
- `video_url`
- one of: `comment_text` or `comment_texts`

Optional columns:
- `order`
- `delay_seconds`
- `account_name`
- `allowed_accounts`
- `eligible_accounts`
- `target_username`

### Field behavior
- `account_name` — bind row to one account (supports account `name` or TikTok username alias).
- `allowed_accounts` / `eligible_accounts` — list separated by `|` or comma.
- `target_username` — when set, the bot tries to reply to a comment from that username.

---

## 8) Sending randomization and limits

Defaults live in `app/config.py` (`load_app_config()` → `SendBehaviorConfig`):
- `daily_limit_min`, `daily_limit_max`
- `hourly_limit_min`, `hourly_limit_max`
- `batch_size_min`, `batch_size_max`
- `batch_pause_min_seconds`, `batch_pause_max_seconds`
- `comment_delay_choices`

Runtime policy logic is in `app/services/send_policy.py`:
- random account limits,
- random batch sizes,
- random comment text variant,
- random delay with jitter,
- cooldown scheduling.

---

## 9) Result statuses (send)

Common statuses you may see:
- `posted` — API/UI flow reports success.
- `posted_unverified` — API success but comment text was not confirmed in UI shortly after posting.
- `publish_timeout` — publish response was not captured in time.
- `batch_error` — account batch crashed; rows were marked failed and run continued.

> Note: TikTok moderation/shadow filtering can still hide comments publicly even if posting endpoint returns success.

---

## 10) Manual login behavior

If session is inactive:
- browser opens login flow,
- you log in manually,
- app re-checks login state (with retries),
- then continues.

If login still appears required after retries, that account fails health-check for the run.

---

## 11) Troubleshooting

### “Comment input could not be found...”
Possible reasons:
- comments disabled on that video,
- panel not opened due UI change,
- temporary verification/challenge,
- account restriction in current session.

### Posted but not visible in TikTok
- could be moderation delay,
- could be shadow filtering,
- use text variants + slower cadence,
- avoid blasting identical comments from many accounts at once.

### Too many browser windows
Session checks intentionally open profiles before main action so failures happen early.

---

## 12) Pre-delivery cleanup

Before sharing project:
- clean `logs/` and `exports/` if not needed,
- remove local `venv/`, `__pycache__/`, `.pytest_cache/`,
- remove real tokens/keys from account JSONs (prefer env variables).
