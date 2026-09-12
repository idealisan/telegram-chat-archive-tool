# tg-down

Download your Telegram **Saved Messages** (`me`) to a local SQLite database and media directory.

## Features

- Saves all messages (text, media metadata) to SQLite
- Downloads media files (photos, videos, documents, audio, voice, stickers, animations)
- Resolves `t.me/…` links — fetches and saves the *linked* message content
- Expands linked album/grouped messages so every photo/video in the group is saved
- Reuses already-downloaded media when the same Telegram file appears again in forwards, repeats, or full scans
- Starts from the **oldest** message; fully resumable if interrupted
- Repair pass on every run: previously failed media downloads and link resolutions are retried automatically — no file is ever silently skipped (`media_status` tracks `failed:*` / `skipped:*` / `gone` per row)
- Optional full-history scan mode to backfill missed messages without clearing old data
- Guards free disk space (configurable minimum) before and during download: when space runs low it pauses and rechecks every minute, resuming automatically once space is freed

## Setup

```bash
.venv/bin/pip install -r requirements.txt
```

Copy the example environment file and fill in your credentials (obtain from <https://my.telegram.org>):

```bash
cp .env.example .env
```

```bash
# .env
API_ID=12345678
API_HASH=your_api_hash_here
```

## Usage

```bash
.venv/bin/python main.py [--env .env] [--full-scan]
```

To re-scan the entire Saved Messages history and backfill anything missing from the database:

```bash
.venv/bin/python main.py --full-scan
```

The first run will ask for your phone number and a Telegram login code (standard MTProto auth). The session is saved locally so subsequent runs skip authentication. If Telegram invalidates that session because it was used from multiple IP addresses, the tool clears the broken session and asks you to log in again.

Interrupt at any time with **Ctrl+C** — progress is saved automatically after every message.

## Config reference (`.env`, see `.env.example`)

| Variable | Default | Description |
|----------|---------|-------------|
| `API_ID` | *(required)* | Telegram API ID |
| `API_HASH` | *(required)* | Telegram API hash |
| `SESSION_NAME` | `tg_session` | Telethon session file name |
| `MEDIA_DIR` | `./media` | Root directory for downloaded files |
| `DB_PATH` | `./messages.db` | SQLite database path |
| `MIN_FREE_SPACE_GB` | `10` | Minimum free disk space (GB) required to continue |
| `DOWNLOAD_MEDIA` | `true` | Whether to download media files at all |
| `MEDIA_TYPES` | all types | Comma-separated media types to download |
| `MAX_FILE_SIZE_MB` | *(empty = no limit)* | Skip media files larger than this |
| `RESOLVE_TME_LINKS` | `true` | Fetch & save messages pointed to by `t.me` links |
| `REQUEST_DELAY_SECONDS` | `0.5` | Sleep between API calls (avoid flood limits) |
| `PROXY` | *(empty = none)* | SOCKS5/HTTP proxy URL, e.g. `socks5://127.0.0.1:1080` (`python-socks[asyncio]` must be installed) |
| `SEARCH_PATHS` | *(empty)* | Comma-separated extra directories to search before re-downloading existing media |
| `SKIP_MISSING_MEDIA` | `false` | Trust DB media paths even if the file is currently missing from disk |
| `GROUP_SCAN_WINDOW` | `12` | When resolving a linked album item, scan this many message IDs before/after it for the rest of the group |

Real environment variables override values in the `.env` file, so secrets can also be injected by the environment (Docker, CI).

## Database schema

**`messages`** — one row per Saved Message (`media_status`: NULL = ok, `failed:*` = retried next run, `skipped:*` = intentionally skipped, `gone` = deleted upstream)  
**`resolved_messages`** — content fetched by following `t.me` links  
**`download_state`** — stores resume cursor (`last_saved_message_id`)  
**`media_index`** — media fingerprint → path dedup index

## Media directory layout

```
media/
  photo/2024/05/123456789.jpg
  video/2024/05/987654321.mp4
  document/2024/06/111111111.pdf
  resolved/<peer>/photo/2024/05/222222222.jpg   # media of t.me-linked messages, namespaced per chat
  ...
```
