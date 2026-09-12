#!/usr/bin/env python3
"""tg-down — Telegram dialog archiver.

Lists all dialogs, lets the user fuzzy-search and pick one with
Up/Down + Enter, then downloads that dialog's full history and media.
"""

import asyncio
import argparse

from telethon.errors import AuthKeyDuplicatedError, FloodWaitError

from utils import load_config, check_disk_space, free_space_gb, wait_for_disk_space
from downloader import _build_client, _reauthorize_client, make_target, run_download
from picker import entity_name, pick_dialog


async def _load_dialogs(client, attempts: int = 4):
    """Fetch all dialogs, waiting through FloodWait rate limits.

    get_dialogs() pages through everything (including archived folders),
    but large accounts can hit FloodWait mid-fetch — retry instead of dying.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await client.get_dialogs()
        except FloodWaitError as e:
            print(f"[PICK] Rate limited while listing dialogs, "
                  f"waiting {e.seconds}s … (attempt {attempt}/{attempts})")
            await asyncio.sleep(e.seconds + 1)
    print("[ERROR] Could not list dialogs after rate-limit retries. "
          "Try again later, or use --peer to open one chat directly.")
    return []


async def _select_target(client, peer_arg):
    """Resolve the download target: --peer value or interactive picker."""
    if peer_arg:
        if peer_arg.strip().lower() == "me":
            return make_target("me")
        entity = await client.get_entity(peer_arg)
        return make_target(entity, name=entity_name(entity))
    dialogs = await _load_dialogs(client)

    async def _resolve_direct(query):
        return await client.get_entity(query)

    chosen = await pick_dialog(dialogs, resolve=_resolve_direct)
    if chosen is None:
        return None
    return make_target(chosen.entity, name=chosen.name)


async def _connect_and_select(cfg, peer_arg):
    """Short-lived client just for target selection (closed afterwards)."""
    client = _build_client(cfg)
    try:
        await client.start()
    except AuthKeyDuplicatedError:
        client = await _reauthorize_client(client, cfg, startup=True)
    try:
        return await _select_target(client, peer_arg)
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description="Download a Telegram dialog's full history and media.")
    parser.add_argument(
        "--full-scan",
        action="store_true",
        help="Scan history from the oldest message and backfill any DB/media gaps.",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="Path to the environment config file (default: .env).",
    )
    parser.add_argument(
        "--peer",
        default=None,
        help="Skip the picker: username, phone, numeric ID, or 'me' for Saved Messages.",
    )
    args = parser.parse_args()

    cfg = load_config(args.env)

    media_dir = cfg["media_dir"]
    min_gb = cfg["min_free_space_gb"]

    print("[CHECK] Verifying disk space …")
    if not check_disk_space(media_dir, min_gb):
        free = free_space_gb(media_dir)
        print(
            f"[WARN] Not enough free disk space "
            f"(need {min_gb} GB, have {free:.2f} GB). "
            f"Waiting for cleanup — download starts automatically."
        )
        wait_for_disk_space(media_dir, min_gb)
    print(f"[OK]    {free_space_gb(media_dir):.1f} GB free on '{media_dir}'.")

    try:
        target = asyncio.run(_connect_and_select(cfg, args.peer))
    except KeyboardInterrupt:
        print("\n[INFO] Aborted by user.")
        return
    if target is None:
        print("[INFO] No dialog selected, exiting.")
        return
    print(f"[PICK] Selected: {target.label} [{target.key}]")

    try:
        asyncio.run(run_download(cfg, target, full_scan=args.full_scan))
    except KeyboardInterrupt:
        print("\n[INFO] Aborted by user.")


if __name__ == "__main__":
    main()
