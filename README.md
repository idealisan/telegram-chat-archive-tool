# tg-down

Download your Telegram **Saved Messages** (`me`) to a local SQLite database and media directory.

## Features

- Saves all messages (text, media metadata) to SQLite
- Downloads media files (photos, videos, documents, audio, voice, stickers, animations)
- Resolves `t.me/…` links — fetches and saves the *linked* message content
- Expands linked album/grouped messages so every photo/video in the group is saved
- Reuses already-downloaded media when the same Telegram file appears again in forwards, repeats, or full scans
- Starts from the **oldest** message; fully resumable if interrupted
- Optional full-history scan mode to backfill missed messages without clearing old data
- Checks for at least 10 GB (configurable) of free disk space before and during download

## Setup

```bash
pip install -r requirements.txt
```

Edit **`config.json`** and fill in your credentials (obtain from <https://my.telegram.org>):

```json
{
    "api_id": "12345678",
    "api_hash": "your_api_hash_here",
    ...
}
```

## Usage

```bash
python main.py
```

To re-scan the entire Saved Messages history and backfill anything missing from the database:

```bash
python main.py --full-scan
```

The first run will ask for your phone number and a Telegram login code (standard MTProto auth). The session is saved locally so subsequent runs skip authentication. If Telegram invalidates that session because it was used from multiple IP addresses, the tool clears the broken session and asks you to log in again.

Interrupt at any time with **Ctrl+C** — progress is saved automatically after every message.

## Config reference

| Key | Default | Description |
|-----|---------|-------------|
| `api_id` | *(required)* | Telegram API ID |
| `api_hash` | *(required)* | Telegram API hash |
| `session_name` | `tg_session` | Telethon session file name |
| `media_dir` | `./media` | Root directory for downloaded files |
| `db_path` | `./messages.db` | SQLite database path |
| `min_free_space_gb` | `10` | Minimum free disk space (GB) required to continue |
| `batch_size` | `100` | Iteration batch size passed to Telethon |
| `download_media` | `true` | Whether to download media files at all |
| `media_types` | all types | Which media types to download |
| `max_file_size_mb` | `null` (no limit) | Skip media files larger than this |
| `resolve_tme_links` | `true` | Fetch & save messages pointed to by `t.me` links |
| `request_delay_seconds` | `0.5` | Sleep between API calls (avoid flood limits) |
| `proxy` | `null` | SOCKS5/HTTP proxy, e.g. `["socks5","127.0.0.1",1080]` (`python-socks[asyncio]` must be installed) |
| `search_paths` | `[]` | Extra directories to search before re-downloading existing media |
| `skip_missing_media` | `false` | Trust DB media paths even if the file is currently missing from disk |
| `group_scan_window` | `12` | When resolving a linked album item, scan this many message IDs before/after it for the rest of the group |

## Database schema

**`messages`** — one row per Saved Message  
**`resolved_messages`** — content fetched by following `t.me` links  
**`download_state`** — stores resume cursor (`last_saved_message_id`)

## Media directory layout

```
media/
  photo/2024/05/123456789.jpg
  video/2024/05/987654321.mp4
  document/2024/06/111111111.pdf
  resolved/<peer>/photo/2024/05/222222222.jpg   # media of t.me-linked messages, namespaced per chat
  ...
```
