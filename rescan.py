"""
rescan.py — Re-index media files into the database.

Run this after moving files to a new location so tg-down
knows where they are and won't re-download them.

Usage:
    python3 rescan.py                  # scan all paths in config
    python3 rescan.py /path/to/media   # scan a specific directory
"""

import sys
from pathlib import Path

import sqlite3
from utils import load_config

import db


def rescan(db_path: str, scan_dirs: list[str]):
    print(f"[RESCAN] Scanning {len(scan_dirs)} director{'y' if len(scan_dirs)==1 else 'ies'} …")

    # Build index: message_id (int) -> list of found paths
    found: dict[int, list[Path]] = {}
    total_files = 0
    for scan_dir in scan_dirs:
        d = Path(scan_dir)
        if not d.exists():
            print(f"  [WARN] Directory not found, skipping: {scan_dir}")
            continue
        for f in d.rglob("*"):
            if f.is_file() and f.suffix != ".tmp":
                # Filename starts with message_id: "123456_name.ext" or "123456.ext"
                stem = f.stem.split("_")[0]
                if stem.isdigit():
                    msg_id = int(stem)
                    found.setdefault(msg_id, []).append(f)
                    total_files += 1

    print(f"[RESCAN] Found {total_files} media files covering {len(found)} message IDs.")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    updated = 0
    ambiguous = 0
    not_in_db = 0

    for msg_id, paths in found.items():
        # Store portable relative paths when the file lives under the first
        # scan dir that contains it (usually media_dir); else absolute.
        def _store_path(p: Path) -> str:
            for scan_dir in scan_dirs:
                try:
                    return str(p.resolve().relative_to(Path(scan_dir).resolve()))
                except (ValueError, OSError):
                    continue
            return str(p)

        rows = conn.execute(
            "SELECT id, media_path FROM messages WHERE message_id = ?", (msg_id,)
        ).fetchall()
        table = "messages"
        if not rows:
            # resolved message IDs repeat across chats — handle every row.
            rows = conn.execute(
                "SELECT id, source_message_id, peer, message_id, media_path"
                " FROM resolved_messages WHERE message_id = ?",
                (msg_id,),
            ).fetchall()
            table = "resolved_messages"

        if not rows:
            not_in_db += 1
            continue

        if len(paths) > 1 or len(rows) > 1:
            # One file <-> one row is the only safe mapping. Several files
            # for one message, or one numeric ID shared by several linked
            # chats, cannot be disambiguated by filename alone.
            print(f"  [AMBIGUOUS] msg {msg_id} matches {len(paths)} file(s), {len(rows)} DB row(s) — skipping:")
            for p in paths:
                print(f"    {p}")
            ambiguous += 1
            continue

        old_path = rows[0]["media_path"]
        new_path = _store_path(paths[0])
        if old_path == new_path:
            continue  # already correct

        db.set_media_path_by_rowid(db_path, table, rows[0]["id"], new_path)
        updated += 1
        if old_path:
            print(f"  [UPDATE] msg {msg_id}: {old_path}  →  {new_path}")
        else:
            print(f"  [NEW]    msg {msg_id}: {new_path}")

    conn.commit()
    conn.close()

    print(
        f"\n[DONE] {updated} path(s) updated, "
        f"{ambiguous} ambiguous (skipped), "
        f"{not_in_db} files not in DB."
    )


if __name__ == "__main__":
    argv = sys.argv[1:]
    env_path = ".env"
    if "--env" in argv:
        i = argv.index("--env")
        try:
            env_path = argv[i + 1]
        except IndexError:
            print("[ERROR] --env requires a path argument.")
            sys.exit(1)
        del argv[i:i + 2]

    cfg = load_config(env_path)
    db_path = cfg["db_path"]

    if argv:
        # Paths passed as arguments
        scan_dirs = argv
    else:
        # Scan media_dir + any extra search_paths from config
        scan_dirs = [cfg["media_dir"]] + cfg.get("search_paths", [])

    rescan(db_path, scan_dirs)
