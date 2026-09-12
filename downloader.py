"""Core downloader for tg-down."""

import asyncio
import mimetypes
import sys
import time
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import AuthKeyDuplicatedError, FloodWaitError
from telethon.tl.types import (
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaWebPage,
    MessageService,
)

import db
import utils


# Network-level exceptions worth retrying
_NETWORK_ERRORS = (
    ConnectionError,
    OSError,
    asyncio.TimeoutError,
    TimeoutError,
)

# Max reconnect attempts before giving up (0 = retry forever)
MAX_RETRIES = 0
RETRY_BASE_DELAY = 5     # seconds
RETRY_MAX_DELAY  = 300   # seconds (5 min cap)

# Parallel download settings
_PARALLEL_THRESHOLD_MB = 5   # files >= 5 MB use parallel chunk download
_PARALLEL_WORKERS = 4        # concurrent chunk-download coroutines
_CHUNK_SIZE = 512 * 1024     # bytes per chunk (must be a multiple of 4 096)

# Progress display settings
_PROGRESS_UPDATE_INTERVAL = 0.75  # seconds between terminal refreshes


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _make_progress_callback(total: int | None, filename: str):
    """Return a Telethon progress_callback that rewrites a single status line."""
    start = time.monotonic()
    last: dict = {
        "received": 0,
        "t": start,
        "shown_received": 0,
        "shown_t": start,
    }

    def callback(received: int, total_bytes: int):
        now = time.monotonic()
        size_total = total_bytes or total or 0
        is_complete = bool(size_total and received >= size_total)
        dt_since_refresh = now - last["shown_t"]

        if not is_complete and dt_since_refresh < _PROGRESS_UPDATE_INTERVAL:
            last["received"] = received
            last["t"] = now
            return

        # Speed: bytes since the last displayed update, not every callback.
        shown_dt = now - last["shown_t"]
        speed = (received - last["shown_received"]) / shown_dt if shown_dt > 0 else 0
        last["received"] = received
        last["t"] = now
        last["shown_received"] = received
        last["shown_t"] = now

        speed_str = f"{_fmt_size(int(speed))}/s" if speed > 0 else "-- B/s"

        if size_total:
            pct = received / size_total * 100
            done = int(pct / 5)               # 20-char bar
            bar = "█" * done + "░" * (20 - done)
            line = (
                f"    ↳ {bar} {pct:5.1f}%  "
                f"{_fmt_size(received)}/{_fmt_size(size_total)}  "
                f"{speed_str}  [{filename}]"
            )
        else:
            line = (
                f"    ↳ {_fmt_size(received)} downloaded  "
                f"{speed_str}  [{filename}]"
            )

        # Truncate to terminal width so the line never wraps
        width = min(120, (getattr(sys.stdout, 'columns', None) or 120))
        sys.stdout.write("\r" + line[:width].ljust(width))
        sys.stdout.flush()

    return callback


def _cleanup_tmp_files(media_dir: str):
    """Remove any leftover .tmp files from interrupted previous runs."""
    count = 0
    for tmp in Path(media_dir).rglob("*.tmp"):
        try:
            tmp.unlink()
            count += 1
        except OSError:
            pass
    if count:
        print(f"[STARTUP] Removed {count} incomplete .tmp file(s) from a previous run.")


def _require_proxy_support(proxy) -> None:
    """Fail fast if proxy support is configured but the async backend is missing."""
    if not proxy:
        return

    try:
        import python_socks.async_.asyncio  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Proxy support requires the 'python-socks[asyncio]' package. "
            "Reinstall dependencies with 'pip install -r requirements.txt'."
        ) from exc


def _build_client(cfg: dict) -> TelegramClient:
    proxy = utils.parse_proxy(cfg.get("proxy"))
    _require_proxy_support(proxy)
    return TelegramClient(
        cfg["session_name"],
        cfg["api_id"],
        cfg["api_hash"],
        proxy=proxy,
    )


def _session_db_path(session_name: str) -> Path:
    session_path = Path(session_name)
    if session_path.suffix == ".session":
        return session_path
    return session_path.with_suffix(".session")


def _delete_session_files(session_name: str) -> None:
    session_path = _session_db_path(session_name)
    extra_paths = [
        session_path,
        Path(f"{session_path}-journal"),
        Path(f"{session_path}-wal"),
        Path(f"{session_path}-shm"),
    ]
    for path in extra_paths:
        if path.exists():
            path.unlink()


async def _reauthorize_client(client: TelegramClient, cfg: dict, *, startup: bool) -> TelegramClient:
    phase = "startup" if startup else "download"
    print(
        f"[AUTH] Telegram invalidated the saved session during {phase} because it "
        "was used from multiple IP addresses."
    )
    await client.disconnect()
    client.session.delete()
    _delete_session_files(cfg["session_name"])
    print("[AUTH] Local session cleared. Please log in again when prompted.")

    new_client = _build_client(cfg)
    await new_client.start()
    return new_client


def _get_media_type(message) -> str | None:
    if not isinstance(message, Message):
        return None
    if message.photo:
        return "photo"
    if message.sticker:
        return "sticker"
    if message.gif:
        return "animation"
    if message.voice:
        return "voice"
    if message.video_note:
        return "video_note"
    if message.audio:
        return "audio"
    if message.video:
        return "video"
    if message.document:
        return "document"
    if message.web_preview:
        return "webpage"
    return None


def _message_to_dict(message) -> dict:
    return {
        "message_id": message.id,
        "date": message.date.isoformat() if message.date else None,
        "text": message.text or message.message,
        "from_id": (
            message.from_id.user_id
            if hasattr(message, "from_id") and message.from_id and hasattr(message.from_id, "user_id")
            else None
        ),
        "reply_to_msg_id": (
            message.reply_to.reply_to_msg_id
            if message.reply_to
            else None
        ),
        "raw_json": message.to_dict(),
    }


def _get_media_fingerprint(message) -> str | None:
    if getattr(message, "photo", None) and getattr(message.photo, "id", None) is not None:
        return f"photo:{message.photo.id}:{getattr(message.photo, 'access_hash', None)}"
    if getattr(message, "document", None) and getattr(message.document, "id", None) is not None:
        return f"document:{message.document.id}:{getattr(message.document, 'access_hash', None)}"
    return None


def _get_existing_media_path(
    db_path: str,
    media_dir: str,
    cfg: dict,
    message,
    *,
    skip_missing: bool,
) -> str | None:
    existing_path = db.get_media_path(db_path, message.id)
    if not existing_path:
        existing_path = db.find_media_path_by_fingerprint(
            db_path,
            _get_media_fingerprint(message),
            media_dir,
            cfg.get("search_paths", []),
        )

    if not existing_path:
        return None

    if skip_missing:
        return existing_path

    resolved = db.resolve_media_path(
        media_dir,
        existing_path,
        cfg.get("search_paths", []),
    )
    return existing_path if resolved else None


async def _expand_grouped_messages(client, peer, message, cfg: dict) -> list[Message]:
    grouped_id = getattr(message, "grouped_id", None)
    if not grouped_id:
        return [message]

    window = max(int(cfg.get("group_scan_window", 12)), 0)
    if window == 0:
        return [message]

    start_id = max(1, message.id - window)
    end_id = message.id + window
    ids = list(range(start_id, end_id + 1))

    fetched = await client.get_messages(peer, ids=ids)
    if not isinstance(fetched, list):
        fetched = [fetched]

    grouped_messages = {
        candidate.id: candidate
        for candidate in fetched
        if isinstance(candidate, Message) and getattr(candidate, "grouped_id", None) == grouped_id
    }
    grouped_messages[message.id] = message
    return [grouped_messages[msg_id] for msg_id in sorted(grouped_messages)]


async def _download_sequential_resumable(client, message, tmp_path: Path, known_size, progress_cb):
    """Sequential download that resumes from an existing partial .tmp file."""
    resume_offset = 0
    if tmp_path.exists():
        raw = tmp_path.stat().st_size
        # Telegram requires offsets aligned to 4 096 bytes
        resume_offset = (raw // 4096) * 4096
        if resume_offset < raw:
            with open(tmp_path, "r+b") as fh:
                fh.truncate(resume_offset)

    open_mode = "ab" if resume_offset > 0 else "wb"
    with open(tmp_path, open_mode) as fh:
        async for chunk in client.iter_download(
            message,
            offset=resume_offset,
            chunk_size=_CHUNK_SIZE,
        ):
            fh.write(chunk)
            resume_offset += len(chunk)
            if progress_cb:
                progress_cb(resume_offset, known_size or 0)


async def _download_parallel_chunks(
    client, message, tmp_path: Path, known_size: int, progress_cb,
    n_workers: int = _PARALLEL_WORKERS,
):
    """Download a large file using N interleaved chunk-download coroutines.

    Each worker i fetches chunks at offsets: i*C, i*C + stride, i*C + 2*stride, …
    where stride = n_workers * chunk_size.  Workers write directly into a
    pre-allocated file using seek, which is safe in asyncio (single-threaded —
    no seek/write pair is ever interrupted by another coroutine).
    """
    chunk_size = _CHUNK_SIZE
    stride = n_workers * chunk_size

    # Pre-allocate so every worker can seek-write at arbitrary offsets.
    with open(tmp_path, "wb") as fh:
        fh.seek(known_size - 1)
        fh.write(b"\x00")

    received_per_worker = [0] * n_workers

    with open(tmp_path, "r+b") as fh:
        async def _worker(wid: int):
            current_offset = wid * chunk_size
            async for chunk in client.iter_download(
                message,
                offset=current_offset,
                stride=stride,
                chunk_size=chunk_size,
            ):
                fh.seek(current_offset)
                fh.write(chunk)
                received_per_worker[wid] += len(chunk)
                if progress_cb:
                    progress_cb(sum(received_per_worker), known_size)
                current_offset += stride

        await asyncio.gather(*[_worker(i) for i in range(n_workers)])


async def _download_media(client, message, media_dir: str, cfg: dict) -> str | None:
    """Download message media and return relative path, or None."""
    media_type = _get_media_type(message)
    if not media_type or media_type == "webpage":
        return None

    allowed = cfg.get("media_types", [])
    if media_type not in allowed:
        return None

    # Check file size limit
    max_mb = cfg.get("max_file_size_mb")
    if max_mb is not None:
        size = None
        if isinstance(message.media, MessageMediaDocument) and message.media.document:
            size = message.media.document.size
        elif isinstance(message.media, MessageMediaPhoto):
            # photos don't expose size directly; skip size check
            pass
        if size is not None and size > max_mb * 1024 * 1024:
            print(f"    [SKIP] Media too large ({size / 1024 / 1024:.1f} MB > {max_mb} MB)")
            return None

    date_str = message.date.strftime("%Y/%m") if message.date else "unknown"
    dest_dir = Path(media_dir) / media_type / date_str
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Build a human-readable filename: {message_id}_{name_hint}{ext}
    # Priority: original document filename > message text snippet > media type
    ext = ""
    original_name = ""

    if isinstance(message.media, MessageMediaDocument) and message.media.document:
        for attr in message.media.document.attributes:
            fname = getattr(attr, "file_name", None)
            if fname:
                p = Path(fname)
                original_name = utils.safe_filename(p.stem)
                ext = p.suffix
                break
        if not ext:
            mime = getattr(message.media.document, "mime_type", "")
            ext = mimetypes.guess_extension(mime) or ""

    if not original_name:
        # Fall back to message text snippet
        text = getattr(message, "text", None) or getattr(message, "message", None) or ""
        if text.strip():
            original_name = utils.safe_filename(text.strip(), max_len=48)

    if original_name:
        filename = f"{message.id}_{original_name}{ext}"
    else:
        filename = f"{message.id}{ext}" if ext else str(message.id)
    dest_path = dest_dir / filename
    tmp_path  = dest_dir / f"{message.id}.tmp"

    # Determine known size for progress bar and cache validation
    known_size = None
    if isinstance(message.media, MessageMediaDocument) and message.media.document:
        known_size = getattr(message.media.document, "size", None)

    # Return cached file only if it exists and has the expected size
    if dest_path.exists():
        if known_size is None or dest_path.stat().st_size == known_size:
            return str(dest_path.relative_to(media_dir))
        else:
            print(f"    [WARN] Cached file size mismatch for msg {message.id}, re-downloading …")
            dest_path.unlink()

    # Check search_paths — user may have moved files to free up space
    for search_dir in cfg.get("search_paths", []):
        for candidate in Path(search_dir).rglob(f"{message.id}_*{ext}"):
            if candidate.is_file():
                if known_size is None or candidate.stat().st_size == known_size:
                    print(f"    [FOUND] Located in search path: {candidate}")
                    return str(candidate)
        # Also try exact filename match without name hint
        candidate = Path(search_dir) / media_type / date_str / filename
        if candidate.is_file():
            if known_size is None or candidate.stat().st_size == known_size:
                print(f"    [FOUND] Located in search path: {candidate}")
                return str(candidate)

    progress = _make_progress_callback(known_size, filename)

    use_parallel = (
        known_size is not None
        and known_size >= _PARALLEL_THRESHOLD_MB * 1024 * 1024
    )
    if use_parallel:
        print(f"    ↳ parallel ({_PARALLEL_WORKERS} workers)  [{filename}]")

    attempt = 0
    while True:
        try:
            if use_parallel:
                if tmp_path.exists():
                    tmp_path.unlink()
                await _download_parallel_chunks(client, message, tmp_path, known_size, progress)
            else:
                await _download_sequential_resumable(client, message, tmp_path, known_size, progress)
            sys.stdout.write("\n")
            sys.stdout.flush()
            tmp_path.rename(dest_path)
            return str(dest_path.relative_to(media_dir))
        except KeyboardInterrupt:
            sys.stdout.write("\n")
            if tmp_path.exists():
                tmp_path.unlink()
            raise
        except FloodWaitError as e:
            sys.stdout.write("\n")
            print(f"    [FLOOD] Rate limited, waiting {e.seconds}s …")
            await asyncio.sleep(e.seconds + 1)
        except _NETWORK_ERRORS as e:
            sys.stdout.write("\n")
            attempt += 1
            wait = min(RETRY_BASE_DELAY * (2 ** (attempt - 1)), RETRY_MAX_DELAY)
            print(f"    [RETRY] Network error on media download (attempt {attempt}): {e}")
            if use_parallel:
                if tmp_path.exists():
                    tmp_path.unlink()
                print(f"    [RETRY] Restarting in {wait}s …")
            else:
                resume_from = tmp_path.stat().st_size if tmp_path.exists() else 0
                print(f"    [RETRY] Resuming from {_fmt_size(resume_from)} in {wait}s …")
            await asyncio.sleep(wait)
        except Exception as exc:
            sys.stdout.write("\n")
            if tmp_path.exists():
                tmp_path.unlink()
            print(f"    [WARN] Failed to download media for msg {message.id}: {exc}")
            return None


async def resolve_and_save(
    client,
    source_message_id: int,
    link_info: dict,
    cfg: dict,
):
    """Fetch the message pointed to by a t.me link and persist it."""
    db_path = cfg["db_path"]
    media_dir = cfg["media_dir"]
    peer = link_info["peer"]
    msg_id = link_info["msg_id"]
    url = link_info["url"]

    try:
        msgs = await client.get_messages(peer, ids=msg_id)
        if not msgs:
            print(f"    [WARN] Could not resolve link {url}")
            return
        linked_msg = msgs if not isinstance(msgs, list) else msgs[0]
        if linked_msg is None:
            print(f"    [WARN] Linked message not found: {url}")
            return

        linked_messages = await _expand_grouped_messages(client, peer, linked_msg, cfg)
        for linked_member in linked_messages:
            data = _message_to_dict(linked_member)
            media_type = _get_media_type(linked_member)
            data["media_fingerprint"] = _get_media_fingerprint(linked_member)
            data["media_type"] = media_type

            if cfg.get("download_media") and media_type:
                existing_path = _get_existing_media_path(
                    db_path,
                    media_dir,
                    cfg,
                    linked_member,
                    skip_missing=cfg.get("skip_missing_media", False),
                )
                if existing_path:
                    data["media_path"] = existing_path
                else:
                    data["media_path"] = await _download_media(client, linked_member, media_dir, cfg)
            else:
                data["media_path"] = None

            db.save_resolved_message(db_path, source_message_id, str(peer), data)

        if len(linked_messages) > 1:
            print(
                f"    [LINK] Saved album from {url} "
                f"({len(linked_messages)} grouped messages)"
            )
        else:
            print(f"    [LINK] Saved resolved content from {url}")

    except FloodWaitError as e:
        print(f"    [FLOOD] Waiting {e.seconds}s for link resolution …")
        await asyncio.sleep(e.seconds + 1)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f"    [WARN] Error resolving {url}: {exc}")


async def run_download(cfg: dict, *, full_scan: bool = False):
    db_path = cfg["db_path"]
    media_dir = cfg["media_dir"]
    delay = cfg["request_delay_seconds"]
    min_gb = cfg["min_free_space_gb"]
    search_paths = cfg.get("search_paths", [])

    db.init_db(db_path)
    Path(media_dir).mkdir(parents=True, exist_ok=True)
    _cleanup_tmp_files(media_dir)

    client = _build_client(cfg)
    try:
        await client.start()
    except AuthKeyDuplicatedError:
        client = await _reauthorize_client(client, cfg, startup=True)
    print("[INFO] Connected to Telegram.")

    # --- Check for files moved/deleted off disk ---
    missing_count = db.get_missing_media_count(db_path, media_dir, search_paths)
    skip_missing = cfg.get("skip_missing_media", False)
    if missing_count and not skip_missing:
        print(
            f"\n[NOTICE] {missing_count} previously downloaded media file(s) are no longer "
            f"on disk (possibly moved to another machine)."
        )
        answer = input("  Skip re-downloading them and keep existing DB records? [y/N]: ").strip().lower()
        if answer == "y":
            skip_missing = True
            print("  [OK] Missing files will be skipped.")
        else:
            print("  [OK] Missing files will be re-downloaded.")
    elif missing_count and skip_missing:
        print(f"[INFO] {missing_count} media file(s) missing from disk — skipping (skip_missing_media=true).")

    total = 0
    reconnect_attempt = 0
    persisted_resume_id = int(db.get_state(db_path, "last_saved_message_id", 0) or 0)
    session_resume_id = 0 if full_scan else persisted_resume_id
    highest_seen_id = persisted_resume_id
    full_scan_completed = False
    try:
        while True:
            try:
                # Reconnect if client dropped
                if not client.is_connected():
                    print("[RECONNECT] Connecting to Telegram …")
                    await client.connect()

                # Read fresh resume cursor (updated after every message)
                min_id = session_resume_id if full_scan else int(
                    db.get_state(db_path, "last_saved_message_id", 0) or 0
                )
                if full_scan:
                    if min_id:
                        print(f"[INFO] Full scan resuming in-memory from message ID > {min_id}")
                    else:
                        print("[INFO] Full scan enabled — scanning from the oldest message.")
                elif min_id:
                    print(f"[INFO] Resuming from message ID > {min_id}")
                else:
                    print("[INFO] Starting from the oldest message.")

                async for message in client.iter_messages(
                    "me",
                    reverse=True,
                    min_id=min_id,
                    limit=None,
                ):
                    # --- Disk space check every 50 messages ---
                    if total % 50 == 0:
                        free = utils.free_space_gb(media_dir)
                        if free < min_gb:
                            print(
                                f"\n[STOP] Disk space too low: {free:.2f} GB free "
                                f"(minimum {min_gb} GB required). Stopping."
                            )
                            return
                        if total % 500 == 0 and total > 0:
                            print(f"[INFO] {total} messages processed, {free:.1f} GB free …")

                    # Skip service messages (user joined, call ended, etc.)
                    if isinstance(message, MessageService):
                        continue

                    highest_seen_id = max(highest_seen_id, message.id)
                    data = _message_to_dict(message)
                    media_type = _get_media_type(message)
                    data["media_fingerprint"] = _get_media_fingerprint(message)
                    date_str = message.date.strftime("%Y-%m-%d") if message.date else "?"
                    text_preview = (data.get("text") or "")[:50].replace("\n", " ")
                    print(f"  [{total+1}] ID={message.id} {date_str} [{media_type or 'text'}] {text_preview}", flush=True)
                    data["media_type"] = media_type
                    data["is_link_resolved"] = False
                    data["resolved_from_url"] = None

                    # --- Download media ---
                    if cfg.get("download_media") and media_type:
                        existing_path = _get_existing_media_path(
                            db_path,
                            media_dir,
                            cfg,
                            message,
                            skip_missing=skip_missing,
                        )
                        if existing_path:
                            data["media_path"] = existing_path
                        else:
                            data["media_path"] = await _download_media(client, message, media_dir, cfg)
                    else:
                        data["media_path"] = None

                    # --- Resolve t.me links ---
                    text = data.get("text") or ""
                    tme_links = utils.extract_tme_links(text) if cfg.get("resolve_tme_links") else []

                    if tme_links:
                        data["is_link_resolved"] = True
                        for link_info in tme_links:
                            await resolve_and_save(client, message.id, link_info, cfg)
                            await asyncio.sleep(delay)

                    if full_scan:
                        db.save_message(db_path, data)
                        session_resume_id = message.id
                    else:
                        db.save_message_and_advance(db_path, data, message.id)
                    total += 1
                    reconnect_attempt = 0  # reset backoff on any success
                    await asyncio.sleep(delay)

                # iter_messages exhausted — all messages downloaded
                if full_scan:
                    db.set_state(db_path, "last_saved_message_id", str(highest_seen_id))
                    full_scan_completed = True
                break

            except KeyboardInterrupt:
                raise  # let outer handler print summary

            except AuthKeyDuplicatedError:
                print()
                client = await _reauthorize_client(client, cfg, startup=False)
                reconnect_attempt = 0
                print("[INFO] Connected to Telegram.")

            except _NETWORK_ERRORS as e:
                reconnect_attempt += 1
                if MAX_RETRIES and reconnect_attempt > MAX_RETRIES:
                    print(f"\n[ERROR] Network error after {MAX_RETRIES} retries: {e}")
                    return
                wait = min(RETRY_BASE_DELAY * (2 ** (reconnect_attempt - 1)), RETRY_MAX_DELAY)
                print(f"\n[RECONNECT] Network error (attempt {reconnect_attempt}): {e}")
                print(f"[RECONNECT] Waiting {wait}s then reconnecting …")
                await asyncio.sleep(wait)

            except Exception as e:
                # Also catch Telethon-specific disconnect errors by class name
                ename = type(e).__name__
                if any(k in ename for k in ("Disconnect", "Connection", "Network", "Timeout")):
                    reconnect_attempt += 1
                    wait = min(RETRY_BASE_DELAY * (2 ** (reconnect_attempt - 1)), RETRY_MAX_DELAY)
                    print(f"\n[RECONNECT] {ename} (attempt {reconnect_attempt}), waiting {wait}s …")
                    await asyncio.sleep(wait)
                else:
                    raise

    except KeyboardInterrupt:
        if full_scan:
            print(
                "\n[INFO] Interrupted. Backfilled messages were kept, "
                "but the incremental resume cursor was not advanced."
            )
        else:
            print("\n[INFO] Interrupted. Progress saved (last message ID stored).")
    finally:
        if full_scan and not full_scan_completed:
            print(
                "\n[INFO] Full scan interrupted before completion. "
                "Existing resume cursor was left unchanged."
            )
        stats = db.get_stats(db_path)
        print(
            f"\n[DONE] Session summary:\n"
            f"  Messages in DB   : {stats['total_messages']}\n"
            f"  With media       : {stats['with_media']}\n"
            f"  Links resolved   : {stats['link_resolved']}\n"
            f"  Linked messages  : {stats['resolved_linked_messages']}\n"
            f"  This run saved   : {total}"
        )
        await client.disconnect()
