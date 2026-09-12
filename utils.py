"""Utility helpers for tg-down."""

import os
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


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _parse_list(value: str | None, default: list[str]) -> list[str]:
    if value is None or str(value).strip() == "":
        return default
    return [item.strip() for item in str(value).split(",") if item.strip()]


def load_config(path: str = ".env") -> dict:
    """Load configuration from a .env file (plus real environment variables).

    Real environment variables take precedence over the file, so containers
    can inject secrets without writing them to disk. See .env.example.
    """
    env_path = Path(path)
    if env_path.exists():
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=env_path, override=False)
    elif not any(os.environ.get(k) for k in ("API_ID", "API_HASH")):
        print(f"[ERROR] Config file not found: {path}")
        print("  Copy '.env.example' to '.env' and fill in your credentials,")
        print("  or export API_ID / API_HASH as environment variables.")
        sys.exit(1)

    api_id = os.environ.get("API_ID", "").strip()
    api_hash = os.environ.get("API_HASH", "").strip()
    if not api_id or not api_hash:
        print(f"[ERROR] 'API_ID' / 'API_HASH' are missing or empty ({path})")
        sys.exit(1)

    try:
        api_id_int = int(api_id)
    except ValueError:
        print(f"[ERROR] 'API_ID' must be a number, got: {api_id!r}")
        sys.exit(1)

    max_size_raw = os.environ.get("MAX_FILE_SIZE_MB", "").strip()
    max_size = None
    if max_size_raw:
        try:
            max_size = float(max_size_raw)
        except ValueError:
            print(f"[ERROR] 'MAX_FILE_SIZE_MB' must be a number, got: {max_size_raw!r}")
            sys.exit(1)

    def _float(key: str, default: float) -> float:
        raw = os.environ.get(key, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            print(f"[ERROR] '{key}' must be a number, got: {raw!r}")
            sys.exit(1)

    def _int(key: str, default: int) -> int:
        raw = os.environ.get(key, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print(f"[ERROR] '{key}' must be an integer, got: {raw!r}")
            sys.exit(1)

    cfg = {
        "api_id": api_id_int,
        "api_hash": api_hash,
        "session_name": os.environ.get("SESSION_NAME", "").strip() or "tg_session",
        "media_dir": os.environ.get("MEDIA_DIR", "").strip() or "./media",
        "db_path": os.environ.get("DB_PATH", "").strip() or "./messages.db",
        "min_free_space_gb": _float("MIN_FREE_SPACE_GB", 10),
        "download_media": _parse_bool(os.environ.get("DOWNLOAD_MEDIA"), True),
        "media_types": _parse_list(
            os.environ.get("MEDIA_TYPES"),
            ["photo", "video", "video_note", "document", "audio", "voice", "sticker", "animation"],
        ),
        "max_file_size_mb": max_size,
        "resolve_tme_links": _parse_bool(os.environ.get("RESOLVE_TME_LINKS"), True),
        "request_delay_seconds": _float("REQUEST_DELAY_SECONDS", 0.5),
        "proxy": os.environ.get("PROXY", "").strip() or None,
        "search_paths": _parse_list(os.environ.get("SEARCH_PATHS"), []),
        "skip_missing_media": _parse_bool(os.environ.get("SKIP_MISSING_MEDIA"), False),
        "group_scan_window": _int("GROUP_SCAN_WINDOW", 12),
    }

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
