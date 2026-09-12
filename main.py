#!/usr/bin/env python3
"""tg-down — Telegram Saved Messages downloader."""

import asyncio
import argparse
import sys

from utils import load_config, check_disk_space, free_space_gb
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
            f"[ERROR] Not enough free disk space.\n"
            f"  Required : {min_gb} GB\n"
            f"  Available: {free:.2f} GB\n"
            f"  Path     : {media_dir}"
        )
        sys.exit(1)
    print(f"[OK]    {free_space_gb(media_dir):.1f} GB free on '{media_dir}'.")

    try:
        asyncio.run(run_download(cfg, full_scan=args.full_scan))
    except KeyboardInterrupt:
        print("\n[INFO] Aborted by user.")


if __name__ == "__main__":
    main()
