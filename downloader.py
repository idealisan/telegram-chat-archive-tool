"""Core downloader for tg-down."""

import asyncio
import mimetypes
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from telethon import TelegramClient
from telethon.errors import AuthKeyDuplicatedError, FloodWaitError
from telethon.tl.types import (
    Channel,
    Chat,
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageService,
    User,
)

import db
import utils


# ---------------------------------------------------------------------------
# Dialog target: everything the pipeline needs for the selected conversation.
# - ref:       entity (or "me") passed to Telethon calls
# - key:       stable DB/cursor key: 'me' | 'user:1' | 'chat:2' | 'channel:3'
# - label:     human-readable dialog name for logs
# - namespace: media-dir segment; None for Saved Messages (legacy layout),
#              'dialogs/<label>_<key>' for everything else
# ---------------------------------------------------------------------------

def peer_key_of(entity) -> str:
    """Stable per-dialog key. Saved Messages (self) is always 'me'."""
    if entity == "me" or getattr(entity, "is_self", False):
        return "me"
    eid = getattr(entity, "id", "?")
    if isinstance(entity, User):
        return f"user:{eid}"
    if isinstance(entity, Channel):
        return f"channel:{eid}"
    if isinstance(entity, Chat):
        return f"chat:{eid}"
    return f"id:{eid}"


def make_target(ref, name: str | None = None) -> SimpleNamespace:
    """Bundle ref + key + label + media namespace for one dialog."""
    key = peer_key_of(ref)
    label = name or ("Saved Messages" if key == "me" else key)
    namespace = None
    if key != "me":
        seg = utils.safe_filename(f"{label}_{key}", max_len=64).replace(":", "-")
        namespace = f"dialogs/{seg or key.replace(':', '-')}"
    return SimpleNamespace(ref=ref, key=key, label=label, namespace=namespace)


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

# Parallel download settings — mirrors the official clients:
# tdesktop uses kMaxFileQueries = 16 in-flight 128 KB parts per file
# (Telegram/SourceFiles/storage/file_download.cpp); MTProto executes the
# concurrent part requests in parallel server-side over the connection.
_PARALLEL_THRESHOLD_MB = 2    # files >= 2 MB use parallel download (16 x 128 KB = one full round)
_PARALLEL_WORKERS = 16        # concurrent in-flight part requests (official: 16)
_CHUNK_SIZE = 128 * 1024      # bytes per part (official kPartSize; must divide 1 MB and be a multiple of 4 096)

# Progress display settings
_PROGRESS_UPDATE_INTERVAL = 0.75  # seconds between terminal refreshes

# Disk-space polling: when the disk fills up, wait this long between
# rechecks instead of aborting — resumes automatically once space is freed.
_DISK_POLL_SECONDS = 60


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
        width = min(120, shutil.get_terminal_size(fallback=(120, 24)).columns)
        sys.stdout.write("\r" + line[:width].ljust(width))
        sys.stdout.flush()

    return callback


async def _wait_for_disk_space(
    media_dir: str,
    min_gb: float,
    poll_seconds: int = _DISK_POLL_SECONDS,
) -> None:
    """Wait until free space is back above *min_gb*, polling periodically.

    Called whenever the disk fills up mid-run (main loop and repair pass):
    downloads pause here and resume automatically once space is freed,
    instead of aborting and requiring a manual rerun.
    """
    while True:
        free = utils.free_space_gb(media_dir)
        if free >= min_gb:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"[DISK] {stamp} Low disk space: {free:.2f} GB free, "
            f"need {min_gb} GB. Downloads paused — rechecking in "
            f"{poll_seconds}s … (Ctrl+C to abort)",
            flush=True,
        )
        await asyncio.sleep(poll_seconds)


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
    from_id = getattr(message, "from_id", None)
    if from_id is not None:
        # PeerUser has user_id; PeerChannel/PeerChat carry channel_id/chat_id.
        from_id = (
            getattr(from_id, "user_id", None)
            or getattr(from_id, "channel_id", None)
            or getattr(from_id, "chat_id", None)
        )
    reply = getattr(message, "reply_to", None)
    return {
        "message_id": message.id,
        "date": message.date.isoformat() if message.date else None,
        "text": message.text or message.message,
        "from_id": from_id,
        "reply_to_msg_id": getattr(reply, "reply_to_msg_id", None),
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
    peer: str,
    *,
    skip_missing: bool,
    allow_same_id: bool = True,
) -> str | None:
    """Return a reusable media path for *message*, or None to force download.

    allow_same_id=False must be used for linked (resolved) messages: their
    numeric IDs live in other chats, so a messages-table hit on the same ID
    would be a different file entirely. Fingerprint matching is still allowed.
    """
    existing_path = (
        db.get_media_path(db_path, peer, message.id) if allow_same_id else None
    )
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


async def _download_media(
    client,
    message,
    media_dir: str,
    cfg: dict,
    *,
    subfolder: str | None = None,
    namespace: str | None = None,
) -> tuple[str | None, str | None]:
    """Download message media; return (relative path, media_status).

    media_status is None on success, 'skipped:size' for intentional size
    skips, or 'failed:<reason>' when the download was attempted but failed
    (the repair pass retries those — nothing is ever silently dropped).

    *subfolder* namespaces files that don't belong to the dialog itself
    (e.g. ``resolved/<peer>`` for t.me-linked content) so identical numeric
    message IDs from different chats can never overwrite each other.
    *namespace* (``dialogs/<label>``) isolates non-Saved-Messages dialogs;
    None keeps the legacy Saved-Messages layout.
    """
    media_type = _get_media_type(message)
    if not media_type or media_type == "webpage":
        return None, None

    allowed = cfg.get("media_types", [])
    if media_type not in allowed:
        return None, "skipped:type"

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
            return None, "skipped:size"

    date_str = message.date.strftime("%Y-%m") if message.date else "unknown"
    base_dir = Path(media_dir)
    if namespace:
        base_dir = base_dir / namespace
    base_dir = base_dir / (subfolder or media_type)
    dest_dir = base_dir / date_str
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
            return str(dest_path.relative_to(media_dir)), None
        else:
            print(f"    [WARN] Cached file size mismatch for msg {message.id}, re-downloading …")
            dest_path.unlink()

    # Check search_paths — user may have moved files to free up space.
    # Paths under media_dir are normalised to relative form so the DB stays
    # portable across machines; anything else stays absolute.
    def _store_path(candidate: Path) -> str:
        try:
            return str(candidate.relative_to(media_dir))
        except ValueError:
            return str(candidate)

    for search_dir in cfg.get("search_paths", []):
        for candidate in Path(search_dir).rglob(f"{message.id}_*{ext}"):
            if candidate.is_file():
                if known_size is None or candidate.stat().st_size == known_size:
                    print(f"    [FOUND] Located in search path: {candidate}")
                    return _store_path(candidate), None
        # Also try exact filename match without name hint
        ns_base = Path(search_dir) / namespace if namespace else Path(search_dir)
        candidate = ns_base / (subfolder or media_type) / date_str / filename
        if candidate.is_file():
            if known_size is None or candidate.stat().st_size == known_size:
                print(f"    [FOUND] Located in search path: {candidate}")
                return _store_path(candidate), None

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
            return str(dest_path.relative_to(media_dir)), None
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
            reason = f"failed:{type(exc).__name__}:{exc}"[:200]
            print(f"    [WARN] Failed to download media for msg {message.id}: {exc}")
            print(f"    [WARN] Marked '{reason}' — the repair pass will retry it next run.")
            return None, reason


async def resolve_and_save(
    client,
    target,
    source_message_id: int,
    link_info: dict,
    cfg: dict,
) -> bool:
    """Fetch the message pointed to by a t.me link and persist it.

    Returns True only if the linked content was actually fetched and stored
    (so the caller can mark the source message resolved honestly). Returns
    False on rate-limit/network errors or failed media downloads — those stay
    unresolved so the repair pass retries them next run. A linked message
    that is definitively gone (deleted / no access) returns True: retrying
    would never succeed.
    """
    db_path = cfg["db_path"]
    media_dir = cfg["media_dir"]
    peer = link_info["peer"]
    msg_id = link_info["msg_id"]
    url = link_info["url"]

    try:
        msgs = await client.get_messages(peer, ids=msg_id)
        if not msgs:
            print(f"    [WARN] Could not resolve link {url}")
            return True
        linked_msg = msgs if not isinstance(msgs, list) else msgs[0]
        if linked_msg is None:
            print(f"    [WARN] Linked message not found: {url}")
            return True

        peer_label = utils.safe_filename(str(peer), max_len=48) or "unknown"
        subfolder = f"resolved/{peer_label}"
        linked_messages = await _expand_grouped_messages(client, peer, linked_msg, cfg)
        all_ok = True
        for linked_member in linked_messages:
            data = _message_to_dict(linked_member)
            media_type = _get_media_type(linked_member)
            data["media_fingerprint"] = _get_media_fingerprint(linked_member)
            data["media_type"] = media_type

            if cfg.get("download_media") and media_type and media_type != "webpage":
                existing_path = _get_existing_media_path(
                    db_path,
                    media_dir,
                    cfg,
                    linked_member,
                    target.key,
                    skip_missing=cfg.get("skip_missing_media", False),
                    allow_same_id=False,
                )
                if existing_path:
                    data["media_path"] = existing_path
                    data["media_status"] = None
                else:
                    path, status = await _download_media(
                        client, linked_member, media_dir, cfg,
                        subfolder=subfolder, namespace=target.namespace,
                    )
                    data["media_path"] = path
                    data["media_status"] = status
                    if media_type and not data["media_path"] and (status or "").startswith("failed"):
                        all_ok = False
            else:
                data["media_path"] = None
                if media_type and media_type != "webpage" and not cfg.get("download_media"):
                    data["media_status"] = "skipped:disabled"
                elif media_type and media_type not in cfg.get("media_types", []):
                    data["media_status"] = "skipped:type"
                else:
                    data["media_status"] = None

            db.save_resolved_message(db_path, target.key, source_message_id, str(peer), data)

        if len(linked_messages) > 1:
            print(
                f"    [LINK] Saved album from {url} "
                f"({len(linked_messages)} grouped messages)"
            )
        else:
            print(f"    [LINK] Saved resolved content from {url}")
        return all_ok

    except FloodWaitError as e:
        print(f"    [FLOOD] Waiting {e.seconds}s for link resolution …")
        await asyncio.sleep(e.seconds + 1)
        return False
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f"    [WARN] Error resolving {url}: {exc}")
        return False


def _coerce_peer(peer: str):
    """Convert a stored peer back to int when it is a numeric channel ID."""
    s = str(peer)
    if s.lstrip("-").isdigit():
        try:
            return int(s)
        except ValueError:
            pass
    return s


async def repair_incomplete(client, target, cfg: dict) -> dict:
    """Retry previously failed media downloads and link resolutions.

    Runs at startup so no file is ever silently skipped: rows marked
    'failed:*' (or left path-less by an older version) are attempted again.
    Intentional skips ('skipped:*') and deleted sources ('gone') are left
    alone. Returns counters for the run summary.
    """
    db_path = cfg["db_path"]
    media_dir = cfg["media_dir"]
    delay = cfg["request_delay_seconds"]
    min_gb = cfg["min_free_space_gb"]
    allowed = cfg.get("media_types", [])
    counts = {"media_repaired": 0, "media_failed": 0, "links_repaired": 0, "links_failed": 0}

    def _single(fetched):
        if isinstance(fetched, list):
            return fetched[0] if fetched else None
        return fetched

    async def _redownload(message, *, subfolder, allow_same_id):
        existing = _get_existing_media_path(
            db_path, media_dir, cfg, message, target.key,
            skip_missing=cfg.get("skip_missing_media", False),
            allow_same_id=allow_same_id,
        )
        if existing:
            return existing, None
        return await _download_media(
            client, message, media_dir, cfg,
            subfolder=subfolder, namespace=target.namespace,
        )

    # --- 1. Failed dialog media ---
    if cfg.get("download_media"):
        pending = db.get_pending_media(db_path, target.key)
        if pending:
            print(f"[REPAIR] Retrying {len(pending)} incomplete media download(s) …")
        for row in pending:
            msg_id = row["message_id"]
            try:
                fetched = _single(await client.get_messages(target.ref, ids=msg_id))
                if fetched is None:
                    db.set_message_media(db_path, target.key, msg_id, None, "gone")
                    print(f"  [REPAIR] msg {msg_id}: source deleted, marked gone.")
                    continue
                await _wait_for_disk_space(media_dir, min_gb)
                media_type = _get_media_type(fetched)
                if not media_type or media_type == "webpage" or media_type not in allowed:
                    db.set_message_media(db_path, target.key, msg_id, None, "skipped:type")
                    continue
                path, status = await _redownload(fetched, subfolder=None, allow_same_id=True)
                db.set_message_media(db_path, target.key, msg_id, path, status)
                if path:
                    counts["media_repaired"] += 1
                    print(f"  [REPAIR] msg {msg_id}: recovered → {path}")
                elif (status or "").startswith("failed"):
                    counts["media_failed"] += 1
            except FloodWaitError as e:
                print(f"  [REPAIR] Flood wait {e.seconds}s, pausing repair …")
                await asyncio.sleep(e.seconds + 1)
            except _NETWORK_ERRORS as e:
                print(f"  [REPAIR] Network error ({e}) — stopping repair, main loop continues; next run retries.")
                break
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"  [REPAIR] msg {msg_id}: still failing ({exc})")
                counts["media_failed"] += 1
            await asyncio.sleep(delay)

        # --- 2. Failed linked-message media ---
        pending_res = db.get_pending_resolved_media(db_path, target.key)
        if pending_res:
            print(f"[REPAIR] Retrying {len(pending_res)} incomplete linked media download(s) …")
        for row in pending_res:
            src, peer_raw, mid = row["source_message_id"], row["peer"], row["message_id"]
            try:
                fetched = _single(await client.get_messages(_coerce_peer(peer_raw), ids=mid))
                if fetched is None:
                    db.set_resolved_media(db_path, target.key, src, peer_raw, mid, None, "gone")
                    continue
                await _wait_for_disk_space(media_dir, min_gb)
                media_type = _get_media_type(fetched)
                if not media_type or media_type == "webpage" or media_type not in allowed:
                    db.set_resolved_media(db_path, target.key, src, peer_raw, mid, None, "skipped:type")
                    continue
                peer_label = utils.safe_filename(str(peer_raw), max_len=48) or "unknown"
                path, status = await _redownload(
                    fetched, subfolder=f"resolved/{peer_label}", allow_same_id=False
                )
                db.set_resolved_media(db_path, target.key, src, peer_raw, mid, path, status)
                if path:
                    counts["media_repaired"] += 1
                elif (status or "").startswith("failed"):
                    counts["media_failed"] += 1
            except FloodWaitError as e:
                print(f"  [REPAIR] Flood wait {e.seconds}s, pausing repair …")
                await asyncio.sleep(e.seconds + 1)
            except _NETWORK_ERRORS as e:
                print(f"  [REPAIR] Network error ({e}) — stopping repair.")
                break
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"  [REPAIR] linked {peer_raw}/{mid}: still failing ({exc})")
                counts["media_failed"] += 1
            await asyncio.sleep(delay)

    # --- 3. Previously unresolved t.me links ---
    if cfg.get("resolve_tme_links"):
        pending_links = db.get_unresolved_links(db_path, target.key)
        if pending_links:
            print(f"[REPAIR] Re-resolving {len(pending_links)} unresolved link message(s) …")
        for row in pending_links:
            try:
                links = utils.extract_tme_links(row["text"] or "")
                if not links:
                    # Mentions t.me but nothing resolvable (e.g. invite link).
                    db.set_link_resolved(db_path, target.key, row["message_id"], True)
                    continue
                ok = True
                for info in links:
                    ok = await resolve_and_save(client, target, row["message_id"], info, cfg) and ok
                    await asyncio.sleep(delay)
                db.set_link_resolved(db_path, target.key, row["message_id"], ok)
                if ok:
                    counts["links_repaired"] += 1
                else:
                    counts["links_failed"] += 1
            except FloodWaitError as e:
                print(f"  [REPAIR] Flood wait {e.seconds}s, pausing repair …")
                await asyncio.sleep(e.seconds + 1)
            except _NETWORK_ERRORS as e:
                print(f"  [REPAIR] Network error ({e}) — stopping repair.")
                break
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"  [REPAIR] links in msg {row['message_id']}: still failing ({exc})")
                counts["links_failed"] += 1
            await asyncio.sleep(delay)

    return counts


async def run_download(cfg: dict, target, *, full_scan: bool = False):
    """Download one dialog's full history + media into the shared DB.

    *target* is a make_target() bundle (ref/key/label/namespace) for the
    selected conversation. Saved Messages uses key 'me' and the legacy
    media layout; every other dialog is namespaced under dialogs/<label>.
    """
    db_path = cfg["db_path"]
    media_dir = cfg["media_dir"]
    delay = cfg["request_delay_seconds"]
    min_gb = cfg["min_free_space_gb"]
    search_paths = cfg.get("search_paths", [])
    cursor = db.cursor_key(target.key)

    db.init_db(db_path)
    Path(media_dir).mkdir(parents=True, exist_ok=True)
    _cleanup_tmp_files(media_dir)

    client = _build_client(cfg)
    try:
        await client.start()
    except AuthKeyDuplicatedError:
        client = await _reauthorize_client(client, cfg, startup=True)
    print("[INFO] Connected to Telegram.")
    print(f"[INFO] Target dialog: {target.label} [{target.key}]")

    # --- Check for files moved/deleted off disk ---
    missing_count = db.get_missing_media_count(db_path, media_dir, search_paths, peer=target.key)
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

    repair_counts = {"media_repaired": 0, "media_failed": 0, "links_repaired": 0, "links_failed": 0}
    total = 0
    reconnect_attempt = 0
    persisted_resume_id = int(db.get_state(db_path, cursor, 0) or 0)
    session_resume_id = 0 if full_scan else persisted_resume_id
    highest_seen_id = persisted_resume_id
    full_scan_completed = False
    try:
        # --- Repair pass: retry anything a previous run failed to fetch ---
        # (inside the outer try so Ctrl+C here still prints the summary and disconnects)
        try:
            repair_counts = await repair_incomplete(client, target, cfg)
        except AuthKeyDuplicatedError:
            print()
            client = await _reauthorize_client(client, cfg, startup=False)
            print("[INFO] Connected to Telegram.")
            repair_counts = await repair_incomplete(client, target, cfg)
        if repair_counts["media_repaired"] or repair_counts["links_repaired"]:
            print(
                f"[REPAIR] Recovered {repair_counts['media_repaired']} media file(s), "
                f"{repair_counts['links_repaired']} link(s)."
            )

        while True:
            try:
                # Reconnect if client dropped
                if not client.is_connected():
                    print("[RECONNECT] Connecting to Telegram …")
                    await client.connect()

                # Read fresh resume cursor (updated after every message)
                min_id = session_resume_id if full_scan else int(
                    db.get_state(db_path, cursor, 0) or 0
                )
                if full_scan:
                    if min_id:
                        print(f"[INFO] Full scan of '{target.label}' resuming in-memory from message ID > {min_id}")
                    else:
                        print(f"[INFO] Full scan enabled — scanning '{target.label}' from the oldest message.")
                elif min_id:
                    print(f"[INFO] Resuming '{target.label}' from message ID > {min_id}")
                else:
                    print(f"[INFO] Starting '{target.label}' from the oldest message.")

                async for message in client.iter_messages(
                    target.ref,
                    reverse=True,
                    min_id=min_id,
                    limit=None,
                ):
                    # --- Disk space gate every 50 messages ---
                    # Low disk pauses (polling every minute) instead of
                    # stopping: downloads resume automatically once space
                    # is freed, with no manual rerun needed.
                    if total % 50 == 0:
                        free = utils.free_space_gb(media_dir)
                        if free < min_gb:
                            print(
                                f"\n[DISK] Disk space too low: {free:.2f} GB free "
                                f"(minimum {min_gb} GB required). Pausing …"
                            )
                            await _wait_for_disk_space(media_dir, min_gb)
                            print("[DISK] Space freed, resuming …")
                        if total % 500 == 0 and total > 0:
                            print(f"[INFO] {total} messages processed, {free:.1f} GB free …")

                    # Skip service messages (user joined, call ended, etc.)
                    # but still advance the resume cursor past them, or every
                    # run would re-scan the same trailing range.
                    if isinstance(message, MessageService):
                        highest_seen_id = max(highest_seen_id, message.id)
                        if full_scan:
                            session_resume_id = message.id
                        else:
                            db.set_state(db_path, cursor, str(message.id))
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
                    # Every outcome is recorded in media_status so the repair
                    # pass can retry real failures. Intentional skips get an
                    # explicit 'skipped:*' status and are never retried.
                    if cfg.get("download_media") and media_type and media_type != "webpage":
                        existing_path = _get_existing_media_path(
                            db_path,
                            media_dir,
                            cfg,
                            message,
                            target.key,
                            skip_missing=skip_missing,
                        )
                        if existing_path:
                            data["media_path"] = existing_path
                            data["media_status"] = None
                        else:
                            path, status = await _download_media(
                                client, message, media_dir, cfg,
                                namespace=target.namespace,
                            )
                            data["media_path"] = path
                            data["media_status"] = status
                    else:
                        data["media_path"] = None
                        if media_type and media_type != "webpage" and not cfg.get("download_media"):
                            data["media_status"] = "skipped:disabled"
                        elif media_type and media_type not in cfg.get("media_types", []):
                            data["media_status"] = "skipped:type"
                        else:
                            data["media_status"] = None

                    # --- Resolve t.me links ---
                    text = data.get("text") or ""
                    tme_links = utils.extract_tme_links(text) if cfg.get("resolve_tme_links") else []

                    if tme_links:
                        links_ok = True
                        for link_info in tme_links:
                            ok = await resolve_and_save(client, target, message.id, link_info, cfg)
                            links_ok = links_ok and ok
                            await asyncio.sleep(delay)
                        # Only mark resolved when every link actually succeeded;
                        # failures stay 0 so the repair pass retries them next run.
                        data["is_link_resolved"] = links_ok

                    if full_scan:
                        db.save_message(db_path, target.key, data)
                        session_resume_id = message.id
                    else:
                        db.save_message_and_advance(db_path, target.key, data, message.id)
                    total += 1
                    reconnect_attempt = 0  # reset backoff on any success
                    await asyncio.sleep(delay)

                # iter_messages exhausted — all messages downloaded
                if full_scan:
                    db.set_state(db_path, cursor, str(highest_seen_id))
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
        stats = db.get_stats(db_path, target.key)
        print(
            f"\n[DONE] Session summary for '{target.label}' [{target.key}]:\n"
            f"  Messages in DB   : {stats['total_messages']}\n"
            f"  With media       : {stats['with_media']}\n"
            f"  Links resolved   : {stats['link_resolved']}\n"
            f"  Linked messages  : {stats['resolved_linked_messages']}\n"
            f"  This run saved   : {total}\n"
            f"  Repaired media   : {repair_counts['media_repaired']}\n"
            f"  Repaired links   : {repair_counts['links_repaired']}\n"
            f"  Still failing    : {stats['pending_media']} media, "
            f"{stats['unresolved_links']} links"
        )
        await client.disconnect()
