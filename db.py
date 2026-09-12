"""SQLite database layer for tg-down."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id        INTEGER UNIQUE NOT NULL,
    date              TEXT,
    text              TEXT,
    media_type        TEXT,
    media_path        TEXT,
    from_id           INTEGER,
    reply_to_msg_id   INTEGER,
    is_link_resolved  INTEGER DEFAULT 0,
    resolved_from_url TEXT,
    raw_json          TEXT,
    downloaded_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resolved_messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_message_id INTEGER NOT NULL,
    peer              TEXT NOT NULL,
    message_id        INTEGER NOT NULL,
    date              TEXT,
    text              TEXT,
    media_type        TEXT,
    media_path        TEXT,
    raw_json          TEXT,
    downloaded_at     TEXT NOT NULL,
    UNIQUE(source_message_id, peer, message_id)
);

CREATE TABLE IF NOT EXISTS download_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_index (
    fingerprint TEXT PRIMARY KEY,
    media_path   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_message_id ON messages(message_id);
CREATE INDEX IF NOT EXISTS idx_resolved_source ON resolved_messages(source_message_id);
"""


@contextmanager
def get_conn(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)


def resolve_media_path(
    media_dir: str,
    media_path: str | None,
    search_paths: list[str] | None = None,
) -> Path | None:
    """Resolve a stored media_path against current and legacy directory layouts."""
    if not media_path:
        return None

    raw_path = Path(media_path).expanduser()
    candidates: list[Path] = []

    if raw_path.is_absolute():
        candidates.append(raw_path)
    else:
        candidates.append(Path(media_dir) / raw_path)
        candidates.append(raw_path)
        for search_dir in search_paths or []:
            candidates.append(Path(search_dir) / raw_path)

    for candidate in candidates:
        if candidate.exists():
            return candidate
        if not candidate.suffix and candidate.parent.exists():
            matches = sorted(candidate.parent.glob(f"{candidate.name}.*"))
            if len(matches) == 1 and matches[0].is_file():
                return matches[0]

    return None


def get_state(db_path: str, key: str, default=None):
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM download_state WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_state(db_path: str, key: str, value: str):
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO download_state (key, value) VALUES (?, ?)",
            (key, str(value)),
        )


def get_missing_media_count(
    db_path: str,
    media_dir: str,
    search_paths: list[str] | None = None,
) -> int:
    """Count DB records that have a media_path but the file no longer exists on disk."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT media_path FROM messages WHERE media_path IS NOT NULL
            UNION ALL
            SELECT media_path FROM resolved_messages WHERE media_path IS NOT NULL
            """
        ).fetchall()
    return sum(
        1
        for r in rows
        if resolve_media_path(media_dir, r["media_path"], search_paths) is None
    )


def get_media_path(db_path: str, message_id: int) -> str | None:
    """Return the recorded media_path for a message, or None."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT media_path FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
    return row["media_path"] if row else None


def _extract_media_fingerprint(raw_json: dict | str | None) -> str | None:
    if raw_json is None:
        return None
    if isinstance(raw_json, str):
        try:
            raw_json = json.loads(raw_json)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw_json, dict):
        return None

    media = raw_json.get("media")
    if not isinstance(media, dict):
        return None

    photo = media.get("photo")
    if isinstance(photo, dict) and photo.get("id") is not None:
        return f"photo:{photo['id']}:{photo.get('access_hash')}"

    document = media.get("document")
    if isinstance(document, dict) and document.get("id") is not None:
        return f"document:{document['id']}:{document.get('access_hash')}"

    return None


def _upsert_media_index(conn, fingerprint: str | None, media_path: str | None):
    if not fingerprint or not media_path:
        return

    conn.execute(
        """
        INSERT OR REPLACE INTO media_index (fingerprint, media_path, updated_at)
        VALUES (?, ?, ?)
        """,
        (fingerprint, media_path, datetime.utcnow().isoformat()),
    )


def find_media_path_by_fingerprint(
    db_path: str,
    fingerprint: str | None,
    media_dir: str,
    search_paths: list[str] | None = None,
) -> str | None:
    if not fingerprint:
        return None

    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT media_path FROM media_index WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if row and resolve_media_path(media_dir, row["media_path"], search_paths):
            return row["media_path"]

        rows = conn.execute(
            """
            SELECT media_path, raw_json FROM messages WHERE media_path IS NOT NULL
            UNION ALL
            SELECT media_path, raw_json FROM resolved_messages WHERE media_path IS NOT NULL
            """
        ).fetchall()

        for candidate in rows:
            candidate_fingerprint = _extract_media_fingerprint(candidate["raw_json"])
            if candidate_fingerprint != fingerprint:
                continue
            if resolve_media_path(media_dir, candidate["media_path"], search_paths) is None:
                continue
            _upsert_media_index(conn, fingerprint, candidate["media_path"])
            return candidate["media_path"]

    return None


def save_message(db_path: str, msg_data: dict):
    now = datetime.utcnow().isoformat()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO messages
                (message_id, date, text, media_type, media_path, from_id,
                 reply_to_msg_id, is_link_resolved, resolved_from_url, raw_json, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg_data["message_id"],
                msg_data.get("date"),
                msg_data.get("text"),
                msg_data.get("media_type"),
                msg_data.get("media_path"),
                msg_data.get("from_id"),
                msg_data.get("reply_to_msg_id"),
                int(msg_data.get("is_link_resolved", False)),
                msg_data.get("resolved_from_url"),
                json.dumps(msg_data.get("raw_json", {}), default=str),
                now,
            ),
        )
        _upsert_media_index(
            conn,
            msg_data.get("media_fingerprint") or _extract_media_fingerprint(msg_data.get("raw_json")),
            msg_data.get("media_path"),
        )



def save_message_and_advance(db_path: str, msg_data: dict, message_id: int):
    """Save message and advance the resume cursor in a single transaction."""
    now = datetime.utcnow().isoformat()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO messages
                (message_id, date, text, media_type, media_path, from_id,
                 reply_to_msg_id, is_link_resolved, resolved_from_url, raw_json, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg_data["message_id"],
                msg_data.get("date"),
                msg_data.get("text"),
                msg_data.get("media_type"),
                msg_data.get("media_path"),
                msg_data.get("from_id"),
                msg_data.get("reply_to_msg_id"),
                int(msg_data.get("is_link_resolved", False)),
                msg_data.get("resolved_from_url"),
                json.dumps(msg_data.get("raw_json", {}), default=str),
                now,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO download_state (key, value) VALUES (?, ?)",
            ("last_saved_message_id", str(message_id)),
        )
        _upsert_media_index(
            conn,
            msg_data.get("media_fingerprint") or _extract_media_fingerprint(msg_data.get("raw_json")),
            msg_data.get("media_path"),
        )


def save_resolved_message(db_path: str, source_message_id: int, peer: str, msg_data: dict):
    now = datetime.utcnow().isoformat()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO resolved_messages
                (source_message_id, peer, message_id, date, text, media_type,
                 media_path, raw_json, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_message_id,
                peer,
                msg_data["message_id"],
                msg_data.get("date"),
                msg_data.get("text"),
                msg_data.get("media_type"),
                msg_data.get("media_path"),
                json.dumps(msg_data.get("raw_json", {}), default=str),
                now,
            ),
        )
        _upsert_media_index(
            conn,
            msg_data.get("media_fingerprint") or _extract_media_fingerprint(msg_data.get("raw_json")),
            msg_data.get("media_path"),
        )


def get_stats(db_path: str) -> dict:
    with get_conn(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        with_media = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE media_path IS NOT NULL"
        ).fetchone()[0]
        resolved = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE is_link_resolved = 1"
        ).fetchone()[0]
        resolved_msgs = conn.execute(
            "SELECT COUNT(*) FROM resolved_messages"
        ).fetchone()[0]
    return {
        "total_messages": total,
        "with_media": with_media,
        "link_resolved": resolved,
        "resolved_linked_messages": resolved_msgs,
    }
