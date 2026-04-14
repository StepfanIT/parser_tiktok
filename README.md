# TikTok Parser MVP

Mini MVP on Python + Playwright with a terminal menu:

1. Collect comments from a TikTok video into CSV.
2. Send comments from CSV to a TikTok video.
3. Exit the app and return later.

## Project structure

- `main.py` - CLI entry point.
- `app/cli.py` - terminal menu and user flow.
- `app/services/comment_service.py` - application layer for collect/send actions.
- `app/integrations/tiktok_client.py` - Playwright automation for TikTok.
- `app/repositories/` - filesystem-backed account and CSV repositories.
- `data/accounts/main_account.json` - single account config for MVP.
- `data/comments/outgoing_comments.csv` - sample outgoing comments file.
- `logs/app.log` - runtime logs.
- `exports/` - scraped comments output.

## Setup

1. Activate the virtual environment.
2. Install dependencies:

```powershell
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe -m playwright install chromium
```

3. Update `data/comments/outgoing_comments.csv` with the real TikTok video URL and the comments you want to send.

## Account file

`data/accounts/main_account.json` keeps browser settings and the path to the saved TikTok session:

- `storage_state_path` - backup of Playwright login state.
- `user_data_dir` - persistent Chromium profile directory used across runs.
- `browser_type` - default `chromium`.
- `browser_channel` - optional Chrome channel if you want to use local Chrome instead of the bundled browser.
- `headless` - `false` by default because TikTok automation is easier to debug in headed mode.
- `bootstrap_login_if_missing` - when `true`, the app opens TikTok login in the persistent profile and lets you refresh the session manually.

On the first "Send comments" run, the app opens TikTok in `user_data_dir`, waits for you to finish authentication once, and then reuses the same browser profile on later runs. `storage_state_path` is still saved as a backup snapshot.

## Running

```powershell
venv\Scripts\python.exe main.py
```

## Notes

- The scraper first tries to capture TikTok comment API responses and falls back to DOM parsing if needed.
- The scraper now opens the comments panel before scrolling. If zero comments are collected, the app raises an error instead of writing an empty CSV with only headers.
- Posting comments relies on TikTok's current web selectors and may need small selector updates if TikTok changes the UI.
- If TikTok shows a verification puzzle, solve it in the opened browser window and then press Enter in the terminal to continue.
- The code is structured in separate layers so later you can add a proper backend, multiple accounts, queues, APIs, retries, or scheduling without rewriting the MVP from scratch.
