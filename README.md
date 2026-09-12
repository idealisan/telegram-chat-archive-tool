# tg-down

Pick any Telegram dialog (users, groups, channels, Saved Messages) from an
interactive list and download its full history and media to a local SQLite
database and media directory.

## Features

- Interactive dialog picker: live keyword search, `↑`/`↓` to move, `Enter` to confirm, `Esc` to cancel (prompt_toolkit)
- Saves all messages (text, media metadata) of the selected dialog to SQLite
- Downloads media files (photos, videos, documents, audio, voice, stickers, animations)
- Resolves `t.me/…` links — fetches and saves the *linked* message content
- Expands linked album/grouped messages so every photo/video in the group is saved
- Reuses already-downloaded media when the same Telegram file appears again in forwards, repeats, or full scans
- Starts from the **oldest** message; fully resumable if interrupted (resume cursor is stored per dialog)
- One shared database for all dialogs, namespaced by peer (`me`, `user:<id>`, `chat:<id>`, `channel:<id>`); old Saved-Messages-only databases migrate automatically
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
.venv/bin/python main.py [--env .env] [--peer @someone] [--full-scan]
```

Without `--peer`, startup shows the dialog picker: type to filter by name,
username or ID, move with `↑`/`↓`, confirm with `Enter`, cancel with `Esc`.
Tips:

- The `@` is optional (`alice` and `@alice` both match); pasting a
  `https://t.me/…` link works too.
- Archived chats are included in the list.
- The list is fetched from main + archived + every custom folder and merged,
  so quiet old chats can't slip through pagination gaps.
- If the chat is not in your dialog list at all (e.g. a public channel you
  never joined), typing its `@username`, numeric ID, or `t.me/c/…` link
  offers a `⟶ 直接打开` row that opens it directly via the API.
  (`t.me/+…` invite links can't be opened this way — join first.)
  With `--peer` (username, phone, numeric ID, or `me`) the picker is skipped —
  useful for scripts and cron jobs.

To re-scan the selected dialog's entire history and backfill anything missing:

```bash
.venv/bin/python main.py --peer @someone --full-scan
```

To diagnose a missing dialog, dump everything loaded at startup (numbered,
one per line — includes peer key and which list each dialog came from):

```bash
.venv/bin/python main.py --debug-dialogs
# or: .venv/bin/python main.py --debug-dialogs /tmp/dialogs.txt
```

The same can be enabled permanently via `DEBUG_DIALOGS_FILE` in `.env`
(`true` = `./dialogs_debug.txt`, or a custom path; empty = off).

To re-index files you moved on disk (scoped to one dialog, `me` by default):

```bash
.venv/bin/python rescan.py [--peer user:123] [/path/to/media]
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

**`messages`** — one row per dialog message, namespaced by `peer` (`media_status`: NULL = ok, `failed:*` = retried next run, `skipped:*` = intentionally skipped, `gone` = deleted upstream)  
**`resolved_messages`** — content fetched by following `t.me` links  
**`download_state`** — stores per-dialog resume cursors (`cursor:<peer>`)  
**`media_index`** — media fingerprint → path dedup index

## Media directory layout

```
media/
  photo/2024-05/123456789.jpg                          # Saved Messages (legacy flat layout)
  video/2024-05/987654321.mp4
  resolved/<peer>/photo/2024-05/222222222.jpg          # t.me-linked media (Saved Messages)
  dialogs/<name>_<key>/photo/2024-05/333333333.jpg     # other dialogs, namespaced per chat
  dialogs/<name>_<key>/resolved/<peer>/...             # their linked media
  ...
```
