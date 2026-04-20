# TikTok Parser MVP

## Structure

- `main.py` - CLI entrypoint.
- `app/cli.py` - menu flow and user interaction.
- `app/services/comment_service.py` - collection and sending scenarios.
- `app/integrations/tiktok_client.py` - Playwright TikTok integration.
- `app/repositories/` - JSON account config and CSV I/O.
- `data/accounts/` - account configs and browser profiles.
- `data/comments/outgoing_comments.csv` - sample CSV for sending comments.
- `exports/` - collected comments output.
- `logs/app.log` - main runtime log.

## Setup

```bash
python -m venv venv
venv/Scripts/Activate.ps1
pip install -r requirements.txt
```

## Run

```bash
python main.py
```

## Menu navigation

In most interactive prompts you can type `0`, `back`, `menu`, or `exit` to return to the main menu.

## Account config

Each account is a separate JSON file inside `data/accounts/`.

Main fields:

- `name` - internal account alias used in CSV restrictions.
- `storage_state_path` - backup storage state path.
- `user_data_dir` - persistent browser profile directory.
- `tiktok_username` - recommended without `@` for better reply filtering.
- `browser_type` - usually `chromium`.
- `browser_channel` - optional browser channel.
- `headless` - usually `false`.
- `slow_mo_ms` - small delay for stability.
- `login_url` - TikTok login page.
- `bootstrap_login_if_missing` - allows manual login in opened profile when session is missing.

## Multi-account flow

For collection/sending/health-check, the CLI asks:

1. Single or multiple accounts.
2. How many accounts to use.
3. Use saved accounts first or choose/create each slot manually.
4. For anti-detect accounts, select provider preset and profile IDs.

Each selected account is session-checked before the main action starts.

## Outgoing CSV format

Required columns:

- `video_url`
- one text column: `comment_text` or `comment_texts`

Optional columns:

- `order`
- `delay_seconds`
- `account_name`
- `allowed_accounts`
- `eligible_accounts`
- `target_username`

Field behavior:

- `account_name`: bind row to one account. You can use account config `name` or TikTok `username`.
- `allowed_accounts` / `eligible_accounts`: account list separated by `|` or comma.
- `target_username`: target comment author username on the video (without `@`).

## Collection modes

In collection mode you can choose:

1. All selected accounts on one video.
2. Each selected account on each listed video.

For multi-video mode, you can paste URLs comma-separated or line-by-line.

## Where to change sending limits

All default sending limits are in `app/config.py` in `load_app_config()`, inside `SendBehaviorConfig`:

- `daily_limit_min`, `daily_limit_max`
- `hourly_limit_min`, `hourly_limit_max`
- `batch_size_min`, `batch_size_max`
- `batch_pause_min_seconds`, `batch_pause_max_seconds`
- `comment_delay_choices`

Adjust these values directly and restart the CLI.

## Randomization behavior

Random send scheduling is controlled by `app/services/send_policy.py`:

- random per-account daily/hourly limits
- random batch sizes
- random comment text variant selection
- random per-comment delay (base interval + jitter)
- random cooldown between batches

## Notes

- If TikTok shows puzzle/verification, solve it manually in the opened browser.
- If TikTok changes selectors/DOM, integration selectors may need updates.
