"""SQLite database layer for tg-down."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


# NOTE: _MESSAGES_TABLE_SQL / _RESOLVED_TABLE_SQL are reused by the
# migration in init_db (peer columns did not exist in older databases).
_MESSAGES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    peer              TEXT NOT NULL DEFAULT 'me',
    message_id        INTEGER NOT NULL,
    date              TEXT,
    text              TEXT,
    media_type        TEXT,
    media_path        TEXT,
    media_status      TEXT,
    from_id           INTEGER,
    reply_to_msg_id   INTEGER,
    is_link_resolved  INTEGER DEFAULT 0,
    resolved_from_url TEXT,
    raw_json          TEXT,
    downloaded_at     TEXT NOT NULL,
    UNIQUE(peer, message_id)
);
"""

_RESOLVED_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS resolved_messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    dialog_peer       TEXT NOT NULL DEFAULT 'me',
    source_message_id INTEGER NOT NULL,
    peer              TEXT NOT NULL,
    message_id        INTEGER NOT NULL,
    date              TEXT,
    text              TEXT,
    media_type        TEXT,
    media_path        TEXT,
    media_status      TEXT,
    raw_json          TEXT,
    downloaded_at     TEXT NOT NULL,
    UNIQUE(dialog_peer, source_message_id, peer, message_id)
);
"""

SCHEMA = (
    _MESSAGES_TABLE_SQL
    + _RESOLVED_TABLE_SQL
    + """
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
)


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
        # Migrate databases created before media_status existed.
        for table in ("messages", "resolved_messages"):
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN media_status TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        _migrate_peer_columns(conn)
        _migrate_cursor_key(conn)


def _column_names(conn, table: str) -> list:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _migrate_peer_columns(conn):
    """Add per-dialog namespacing to pre-existing databases.

    Old tables keyed rows by message_id alone, which collides across chats.
    Rebuild both tables with peer/dialog_peer columns (old rows belong to
    Saved Messages, i.e. peer 'me'). Idempotent: skipped once migrated.
    """
    if "peer" not in _column_names(conn, "messages"):
        conn.execute("ALTER TABLE messages RENAME TO messages_legacy")
        conn.execute(_MESSAGES_TABLE_SQL)
        conn.execute(
            """
            INSERT INTO messages
                (id, peer, message_id, date, text, media_type, media_path,
                 media_status, from_id, reply_to_msg_id, is_link_resolved,
                 resolved_from_url, raw_json, downloaded_at)
            SELECT id, 'me', message_id, date, text, media_type, media_path,
                 media_status, from_id, reply_to_msg_id, is_link_resolved,
                 resolved_from_url, raw_json, downloaded_at
            FROM messages_legacy
            """
        )
        conn.execute("DROP TABLE messages_legacy")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_message_id ON messages(message_id)"
        )
    if "dialog_peer" not in _column_names(conn, "resolved_messages"):
        conn.execute("ALTER TABLE resolved_messages RENAME TO resolved_messages_legacy")
        conn.execute(_RESOLVED_TABLE_SQL)
        conn.execute(
            """
            INSERT INTO resolved_messages
                (id, dialog_peer, source_message_id, peer, message_id, date,
                 text, media_type, media_path, media_status, raw_json, downloaded_at)
            SELECT id, 'me', source_message_id, peer, message_id, date,
                 text, media_type, media_path, media_status, raw_json, downloaded_at
            FROM resolved_messages_legacy
            """
        )
        conn.execute("DROP TABLE resolved_messages_legacy")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_resolved_source "
            "ON resolved_messages(source_message_id)"
        )


def _migrate_cursor_key(conn):
    """Move the legacy global resume cursor to the per-dialog key for 'me'."""
    legacy = conn.execute(
        "SELECT value FROM download_state WHERE key = 'last_saved_message_id'"
    ).fetchone()
    if legacy is None:
        return
    current = conn.execute(
        "SELECT value FROM download_state WHERE key = 'cursor:me'"
    ).fetchone()
    if current is None:
        conn.execute(
            "INSERT INTO download_state (key, value) VALUES ('cursor:me', ?)",
            (legacy["value"],),
        )
    conn.execute(
        "DELETE FROM download_state WHERE key = 'last_saved_message_id'"
    )


def cursor_key(peer: str) -> str:
    """Resume-cursor key in download_state for one dialog."""
    return f"cursor:{peer}"


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
    peer: str | None = None,
) -> int:
    """Count DB records that have a media_path but the file no longer exists on disk."""
    with get_conn(db_path) as conn:
        if peer is None:
            rows = conn.execute(
                """
                SELECT media_path FROM messages WHERE media_path IS NOT NULL
                UNION ALL
                SELECT media_path FROM resolved_messages WHERE media_path IS NOT NULL
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT media_path FROM messages WHERE peer = ? AND media_path IS NOT NULL
                UNION ALL
                SELECT media_path FROM resolved_messages WHERE dialog_peer = ? AND media_path IS NOT NULL
                """,
                (peer, peer),
            ).fetchall()
    return sum(
        1
        for r in rows
        if resolve_media_path(media_dir, r["media_path"], search_paths) is None
    )


def get_media_path(db_path: str, peer: str, message_id: int) -> str | None:
    """Return the recorded media_path for a message in one dialog, or None."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT media_path FROM messages WHERE peer = ? AND message_id = ?",
            (peer, message_id),
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


def get_resolved_media_path(
    db_path: str,
    dialog_peer: str,
    source_message_id: int,
    peer: str,
    message_id: int,
) -> str | None:
    """Return the recorded media_path for one resolved (linked) message.

    NOTE: resolved message IDs are only unique per (dialog, source, peer) —
    never look them up by message_id alone, or rows from unrelated chats collide.
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT media_path FROM resolved_messages "
            "WHERE dialog_peer = ? AND source_message_id = ? AND peer = ? AND message_id = ?",
            (dialog_peer, source_message_id, str(peer), message_id),
        ).fetchone()
    return row["media_path"] if row else None


def set_media_path_by_rowid(
    db_path: str,
    table: str,
    rowid: int,
    media_path: str,
):
    """Update one row's media_path (by rowid) and keep the media_index in sync.

    A relocated file is no longer a failure, so any previous media_status
    (e.g. 'failed:…') is cleared.
    """
    assert table in ("messages", "resolved_messages"), table
    with get_conn(db_path) as conn:
        row = conn.execute(
            f"SELECT raw_json FROM {table} WHERE id = ?", (rowid,)
        ).fetchone()
        conn.execute(
            f"UPDATE {table} SET media_path = ?, media_status = NULL WHERE id = ?",
            (media_path, rowid),
        )
        fingerprint = _extract_media_fingerprint(row["raw_json"]) if row else None
        _upsert_media_index(conn, fingerprint, media_path)


def save_message(db_path: str, peer: str, msg_data: dict):
    now = datetime.utcnow().isoformat()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO messages
                (peer, message_id, date, text, media_type, media_path, media_status, from_id,
                 reply_to_msg_id, is_link_resolved, resolved_from_url, raw_json, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                peer,
                msg_data["message_id"],
                msg_data.get("date"),
                msg_data.get("text"),
                msg_data.get("media_type"),
                msg_data.get("media_path"),
                msg_data.get("media_status"),
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



def save_message_and_advance(db_path: str, peer: str, msg_data: dict, message_id: int):
    """Save message and advance the dialog's resume cursor in a single transaction."""
    now = datetime.utcnow().isoformat()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO messages
                (peer, message_id, date, text, media_type, media_path, media_status, from_id,
                 reply_to_msg_id, is_link_resolved, resolved_from_url, raw_json, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                peer,
                msg_data["message_id"],
                msg_data.get("date"),
                msg_data.get("text"),
                msg_data.get("media_type"),
                msg_data.get("media_path"),
                msg_data.get("media_status"),
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
            (cursor_key(peer), str(message_id)),
        )
        _upsert_media_index(
            conn,
            msg_data.get("media_fingerprint") or _extract_media_fingerprint(msg_data.get("raw_json")),
            msg_data.get("media_path"),
        )


def save_resolved_message(
    db_path: str,
    dialog_peer: str,
    source_message_id: int,
    peer: str,
    msg_data: dict,
):
    now = datetime.utcnow().isoformat()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO resolved_messages
                (dialog_peer, source_message_id, peer, message_id, date, text, media_type,
                 media_path, media_status, raw_json, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dialog_peer,
                source_message_id,
                peer,
                msg_data["message_id"],
                msg_data.get("date"),
                msg_data.get("text"),
                msg_data.get("media_type"),
                msg_data.get("media_path"),
                msg_data.get("media_status"),
                json.dumps(msg_data.get("raw_json", {}), default=str),
                now,
            ),
        )
        _upsert_media_index(
            conn,
            msg_data.get("media_fingerprint") or _extract_media_fingerprint(msg_data.get("raw_json")),
            msg_data.get("media_path"),
        )


# ---------------------------------------------------------------------------
# Completeness helpers: everything below exists so no file is ever silently
# skipped. media_status conventions:
#   NULL               ok, or no media to download
#   'failed:<reason>'  attempted but failed → retried by the repair pass
#   'skipped:size' / 'skipped:type' / 'skipped:disabled' → intentional, never retried
#   'gone'             source message deleted server-side → never retried
# ---------------------------------------------------------------------------

def set_message_media(
    db_path: str,
    peer: str,
    message_id: int,
    media_path: str | None,
    media_status: str | None,
):
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE messages SET media_path = ?, media_status = ? "
            "WHERE peer = ? AND message_id = ?",
            (media_path, media_status, peer, message_id),
        )


def set_resolved_media(
    db_path: str,
    dialog_peer: str,
    source_message_id: int,
    peer: str,
    message_id: int,
    media_path: str | None,
    media_status: str | None,
):
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE resolved_messages SET media_path = ?, media_status = ? "
            "WHERE dialog_peer = ? AND source_message_id = ? AND peer = ? AND message_id = ?",
            (media_path, media_status, dialog_peer, source_message_id, str(peer), message_id),
        )


def set_link_resolved(db_path: str, peer: str, message_id: int, resolved: bool):
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE messages SET is_link_resolved = ? WHERE peer = ? AND message_id = ?",
            (int(resolved), peer, message_id),
        )


def _needs_media_clause() -> str:
    # Rows whose media still needs (re-)downloading: explicit failures, plus
    # legacy rows saved before media_status existed (NULL status, no path,
    # but a downloadable media type recorded).
    return (
        "(media_status LIKE 'failed%' OR (media_status IS NULL "
        "AND media_path IS NULL AND media_type IS NOT NULL "
        "AND media_type != 'webpage'))"
    )


def get_pending_media(db_path: str, peer: str) -> list:
    """One dialog's rows whose media still needs downloading."""
    with get_conn(db_path) as conn:
        return conn.execute(
            f"SELECT message_id, media_type FROM messages "
            f"WHERE peer = ? AND {_needs_media_clause()} "
            "ORDER BY message_id",
            (peer,),
        ).fetchall()


def get_pending_resolved_media(db_path: str, dialog_peer: str) -> list:
    """One dialog's linked-message rows whose media still needs downloading."""
    with get_conn(db_path) as conn:
        return conn.execute(
            f"SELECT source_message_id, peer, message_id, media_type "
            f"FROM resolved_messages WHERE dialog_peer = ? AND {_needs_media_clause()} "
            "ORDER BY source_message_id, peer, message_id",
            (dialog_peer,),
        ).fetchall()


def get_unresolved_links(db_path: str, peer: str) -> list:
    """One dialog's rows flagged unresolved that mention a t.me link."""
    with get_conn(db_path) as conn:
        return conn.execute(
            "SELECT message_id, text FROM messages "
            "WHERE peer = ? AND is_link_resolved = 0 AND text LIKE '%t.me/%' "
            "ORDER BY message_id",
            (peer,),
        ).fetchall()


def get_stats(db_path: str, peer: str) -> dict:
    """Statistics scoped to one dialog (peer key)."""
    with get_conn(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE peer = ?", (peer,)
        ).fetchone()[0]
        with_media = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE peer = ? AND media_path IS NOT NULL",
            (peer,),
        ).fetchone()[0]
        resolved = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE peer = ? AND is_link_resolved = 1",
            (peer,),
        ).fetchone()[0]
        resolved_msgs = conn.execute(
            "SELECT COUNT(*) FROM resolved_messages WHERE dialog_peer = ?", (peer,)
        ).fetchone()[0]
        failed_media = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE peer = ? AND media_status LIKE 'failed%'",
            (peer,),
        ).fetchone()[0]
        failed_media += conn.execute(
            "SELECT COUNT(*) FROM resolved_messages "
            "WHERE dialog_peer = ? AND media_status LIKE 'failed%'",
            (peer,),
        ).fetchone()[0]
        pending_media = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE peer = ? AND {_needs_media_clause()}",
            (peer,),
        ).fetchone()[0]
        pending_media += conn.execute(
            f"SELECT COUNT(*) FROM resolved_messages "
            f"WHERE dialog_peer = ? AND {_needs_media_clause()}",
            (peer,),
        ).fetchone()[0]
        unresolved_links = conn.execute(
            "SELECT COUNT(*) FROM messages "
            "WHERE peer = ? AND is_link_resolved = 0 AND text LIKE '%t.me/%'",
            (peer,),
        ).fetchone()[0]
    return {
        "total_messages": total,
        "with_media": with_media,
        "link_resolved": resolved,
        "resolved_linked_messages": resolved_msgs,
        "failed_media": failed_media,
        "pending_media": pending_media,
        "unresolved_links": unresolved_links,
    }
