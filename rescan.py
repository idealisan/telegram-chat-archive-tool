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
            if f.is_file() and not f.suffix == ".tmp":
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
        row = conn.execute(
            "SELECT media_path FROM messages WHERE message_id = ?", (msg_id,)
        ).fetchone()

        if row is None:
            # Also check resolved_messages
            rrow = conn.execute(
                "SELECT media_path FROM resolved_messages WHERE message_id = ?", (msg_id,)
            ).fetchone()
            if rrow is None:
                not_in_db += 1
                continue
            table = "resolved_messages"
            old_path = rrow["media_path"]
        else:
            table = "messages"
            old_path = row["media_path"]

        if len(paths) > 1:
            print(f"  [AMBIGUOUS] msg {msg_id} matches {len(paths)} files — skipping:")
            for p in paths:
                print(f"    {p}")
            ambiguous += 1
            continue

        new_path = str(paths[0])
        if old_path == new_path:
            continue  # already correct

        conn.execute(
            f"UPDATE {table} SET media_path = ? WHERE message_id = ?",
            (new_path, msg_id),
        )
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
    cfg = load_config("config.json")
    db_path = cfg["db_path"]

    if len(sys.argv) > 1:
        # Paths passed as arguments
        scan_dirs = sys.argv[1:]
    else:
        # Scan media_dir + any extra search_paths from config
        scan_dirs = [cfg["media_dir"]] + cfg.get("search_paths", [])

    rescan(db_path, scan_dirs)
