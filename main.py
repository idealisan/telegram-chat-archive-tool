#!/usr/bin/env python3
"""tg-down — Telegram dialog archiver.

Lists all dialogs, lets the user fuzzy-search and pick one with
Up/Down + Enter, then downloads that dialog's full history and media.
"""

import asyncio
import argparse

from telethon.errors import AuthKeyDuplicatedError, FloodWaitError
from telethon.tl import functions

from utils import load_config, check_disk_space, free_space_gb, wait_for_disk_space
from downloader import (
    _build_client,
    _reauthorize_client,
    make_target,
    peer_key_of,
    run_download,
)
from picker import entity_name, format_dialog_line, pick_dialog
from pathlib import Path


async def _fetch_round(client, label: str, attempts: int, **kwargs):
    """Fetch one dialog list (main / archived / folder), waiting out FloodWaits."""
    for attempt in range(1, attempts + 1):
        try:
            return await client.get_dialogs(**kwargs)
        except FloodWaitError as e:
            print(f"[PICK] Rate limited while listing dialogs ({label}), "
                  f"waiting {e.seconds}s … (attempt {attempt}/{attempts})")
            await asyncio.sleep(e.seconds + 1)
    print(f"[ERROR] Giving up on dialog list '{label}' after rate-limit retries.")
    return []


def _dialog_dedup_key(dialog) -> str:
    try:
        return peer_key_of(getattr(dialog, "entity", None))
    except Exception:
        return f"raw:{getattr(dialog, 'id', '?')}"


def merge_dialog_rounds(rounds: list) -> tuple:
    """Merge [(label, dialogs)] into (unique dialogs, {peer_key: [labels]}).

    Pure function (no network) so it can be unit-tested.
    """
    merged, origins = [], {}
    for label, dialogs in rounds:
        for dialog in dialogs or []:
            key = _dialog_dedup_key(dialog)
            if key in origins:
                origins[key].append(label)
            else:
                origins[key] = [label]
                merged.append(dialog)
    return merged, origins


async def _load_dialogs(client, attempts: int = 4):
    """Fetch main + archived + custom-folder dialog lists and merge them.

    A single get_dialogs() is documented to return everything, but quiet
    old chats living in Archive / custom folders are exactly the ones that
    go missing in practice — so fetch each source explicitly and dedup.
    """
    rounds = [("main", await _fetch_round(client, "main", attempts))]
    rounds.append(("archived", await _fetch_round(client, "archived", attempts, archived=True)))
    try:
        filters = await client(functions.messages.GetDialogFiltersRequest())
        for dialog_filter in getattr(filters, "filters", []) or []:
            fid = getattr(dialog_filter, "id", None)
            if isinstance(fid, int) and fid not in (0, 1):
                rounds.append((
                    f"folder:{fid}",
                    await _fetch_round(client, f"folder:{fid}", attempts, folder=fid),
                ))
    except Exception as exc:
        print(f"[PICK] Could not list custom folders ({exc}); continuing.")
    merged, origins = merge_dialog_rounds(rounds)
    counts = ", ".join(f"{label}={len(dl or [])}" for label, dl in rounds)
    print(f"[PICK] Loaded {len(merged)} unique dialogs ({counts}).")
    return merged, origins


def _dump_dialogs(path: str, dialogs, origins: dict) -> None:
    """Write every loaded dialog to *path*, numbered, one per line."""
    lines = []
    for i, dialog in enumerate(dialogs, 1):
        key = _dialog_dedup_key(dialog)
        src = "+".join(origins.get(key, ["?"]))
        lines.append(format_dialog_line(i, dialog, key, src))
    target = Path(path)
    if target.parent != Path("."):
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[DEBUG] Wrote {len(lines)} dialogs to {path}")


async def _select_target(client, peer_arg, debug_path=None):
    """Resolve the download target: --peer value or interactive picker."""
    if peer_arg:
        if peer_arg.strip().lower() == "me":
            return make_target("me")
        entity = await client.get_entity(peer_arg)
        return make_target(entity, name=entity_name(entity))
    dialogs, origins = await _load_dialogs(client)
    if debug_path:
        _dump_dialogs(debug_path, dialogs, origins)

    async def _resolve_direct(query):
        return await client.get_entity(query)

    chosen = await pick_dialog(dialogs, resolve=_resolve_direct)
    if chosen is None:
        return None
    return make_target(chosen.entity, name=chosen.name)


async def _connect_and_select(cfg, peer_arg, debug_path=None):
    """Short-lived client just for target selection (closed afterwards)."""
    client = _build_client(cfg)
    try:
        await client.start()
    except AuthKeyDuplicatedError:
        client = await _reauthorize_client(client, cfg, startup=True)
    try:
        return await _select_target(client, peer_arg, debug_path)
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
    parser.add_argument(
        "--debug-dialogs",
        nargs="?",
        const=True,
        default=None,
        help="Dump all loaded dialogs (numbered, one per line) to a file for "
             "diagnosis. Optional path; defaults to DEBUG_DIALOGS_FILE or "
             "./dialogs_debug.txt.",
    )
    args = parser.parse_args()

    cfg = load_config(args.env)

    debug_path = cfg.get("debug_dialogs_file")
    if args.debug_dialogs is True:
        debug_path = debug_path or "./dialogs_debug.txt"
    elif isinstance(args.debug_dialogs, str):
        debug_path = args.debug_dialogs

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
        target = asyncio.run(_connect_and_select(cfg, args.peer, debug_path))
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
