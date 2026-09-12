#!/usr/bin/env python3
"""tg-down — Telegram Saved Messages downloader."""

import asyncio
import argparse

from utils import load_config, check_disk_space, free_space_gb, wait_for_disk_space
from downloader import run_download


def main():
    parser = argparse.ArgumentParser(description="Download Telegram Saved Messages and media.")
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
        asyncio.run(run_download(cfg, full_scan=args.full_scan))
    except KeyboardInterrupt:
        print("\n[INFO] Aborted by user.")


if __name__ == "__main__":
    main()
