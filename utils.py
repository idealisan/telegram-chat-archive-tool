"""Utility helpers for tg-down."""

import json
import re
import shutil
import sys
from pathlib import Path


TME_PATTERNS = [
    # https://t.me/username/123
    re.compile(r"https?://t\.me/(?P<username>[A-Za-z0-9_]{3,})/(?P<msg_id>\d+)"),
    # https://t.me/c/1234567890/123  (private channels)
    re.compile(r"https?://t\.me/c/(?P<channel_id>\d+)/(?P<msg_id>\d+)"),
]


def load_config(path: str = "config.json") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        print(f"[ERROR] Config file not found: {path}")
        sys.exit(1)
    with open(config_path) as f:
        cfg = json.load(f)

    required = ["api_id", "api_hash"]
    for key in required:
        if not cfg.get(key):
            print(f"[ERROR] '{key}' is missing or empty in {path}")
            sys.exit(1)

    # Normalise types
    cfg["api_id"] = int(cfg["api_id"])
    cfg.setdefault("session_name", "tg_session")
    cfg.setdefault("media_dir", "./media")
    cfg.setdefault("db_path", "./messages.db")
    cfg.setdefault("min_free_space_gb", 10)
    cfg.setdefault("batch_size", 100)
    cfg.setdefault("download_media", True)
    cfg.setdefault("media_types", ["photo", "video", "video_note", "document", "audio", "voice", "sticker", "animation"])
    cfg.setdefault("max_file_size_mb", None)
    cfg.setdefault("resolve_tme_links", True)
    cfg.setdefault("request_delay_seconds", 0.5)
    cfg.setdefault("proxy", None)
    cfg.setdefault("search_paths", [])  # extra directories to check before re-downloading
    cfg.setdefault("skip_missing_media", False)  # if True, don't re-download files missing from disk
    cfg.setdefault("group_scan_window", 12)  # scan +/- N message IDs around a linked album item

    return cfg


def check_disk_space(path: str, min_gb: float) -> bool:
    """Return True if free space on the volume containing *path* >= min_gb."""
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(target)
    free_gb = usage.free / (1024 ** 3)
    return free_gb >= min_gb


def free_space_gb(path: str) -> float:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(target).free / (1024 ** 3)


def extract_tme_links(text: str) -> list[dict]:
    """Return a list of parsed t.me link dicts found in *text*."""
    if not text:
        return []
    results = []
    for pat in TME_PATTERNS:
        for m in pat.finditer(text):
            info = {"url": m.group(0), "msg_id": int(m.group("msg_id"))}
            if "username" in m.groupdict() and m.group("username"):
                info["peer"] = m.group("username")
            else:
                # Private channel: peer id needs -100 prefix for Telethon
                info["peer"] = int("-100" + m.group("channel_id"))
            results.append(info)
    return results


def parse_proxy(proxy) -> tuple | None:
    """Convert proxy config to a (type, host, port) tuple Telethon understands.

    Accepts:
      null / None          → no proxy
      [type, host, port]   → used as-is
      "socks5://host:port" → parsed automatically
      "socks://host:port"  → treated as socks5
      "http://host:port"   → HTTP proxy
    """
    if not proxy:
        return None
    if isinstance(proxy, (list, tuple)):
        return tuple(proxy)
    if isinstance(proxy, str):
        from urllib.parse import urlparse
        p = urlparse(proxy)
        scheme = p.scheme.lower()
        host = p.hostname
        port = p.port
        if scheme in ("socks5", "socks"):
            ptype = "socks5"
        elif scheme == "socks4":
            ptype = "socks4"
        elif scheme in ("http", "https"):
            ptype = "http"
        else:
            print(f"[WARN] Unknown proxy scheme '{scheme}', ignoring proxy.")
            return None
        if not host or not port:
            print(f"[WARN] Could not parse proxy URL '{proxy}', ignoring.")
            return None
        return (ptype, host, port)
    print(f"[WARN] Unrecognised proxy format ({type(proxy).__name__}), ignoring.")
    return None


def safe_filename(name: str, max_len: int = 64) -> str:
    """Strip characters unsafe for filenames and collapse whitespace."""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
    name = re.sub(r'[ _]{2,}', "_", name)
    name = name.strip(". _")
    return name[:max_len].rstrip(". _")
