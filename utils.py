"""Utility helpers for tg-down."""

import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


TME_PATTERNS = [
    re.compile(
        r"(?<![A-Za-z0-9_/?=&.\-@:])"
        r"(?P<url>(?:https?://)?(?:www\.)?"
        r"(?:t\.me|telegram\.me|telegram\.dog)/"
        r"(?:(?:c/\d+|s/[A-Za-z0-9_]{3,}|[A-Za-z0-9_]{3,})/\d+)/?)"
        r"(?!\.[A-Za-z0-9]|[-][A-Za-z0-9])"
        r"(?=$|[^A-Za-z0-9_/])",
        re.IGNORECASE,
    )
]
_TME_HOSTS = {
    "t.me",
    "www.t.me",
    "telegram.me",
    "www.telegram.me",
    "telegram.dog",
    "www.telegram.dog",
}


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _parse_list(value: str | None, default: list[str]) -> list[str]:
    if value is None or str(value).strip() == "":
        return default
    return [item.strip() for item in str(value).split(",") if item.strip()]


DEBUG_DIALOGS_DEFAULT_PATH = "./dialogs_debug.txt"


def _parse_debug_dialogs_file(value: str | None) -> str | None:
    """Parse DEBUG_DIALOGS_FILE: empty/false = off, true-like = default path,
    anything else = custom dump path."""
    raw = (value or "").strip()
    if not raw or raw.lower() in ("0", "false", "no", "off"):
        return None
    if raw.lower() in ("1", "true", "yes", "y", "on"):
        return DEBUG_DIALOGS_DEFAULT_PATH
    return raw


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
        "debug_dialogs_file": _parse_debug_dialogs_file(os.environ.get("DEBUG_DIALOGS_FILE")),
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


def wait_for_disk_space(path: str, min_gb: float, poll_seconds: int = 60) -> None:
    """Block until free space on *path*'s volume is >= min_gb.

    Polls every *poll_seconds* instead of aborting, so tg-down resumes
    automatically once space is freed — no manual rerun needed.
    Ctrl+C aborts the wait.
    """
    while True:
        free = free_space_gb(path)
        if free >= min_gb:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"[DISK] {stamp} Low disk space: {free:.2f} GB free, "
            f"need {min_gb} GB. Rechecking in {poll_seconds}s … "
            f"(free up space to resume automatically, Ctrl+C to abort)",
            flush=True,
        )
        time.sleep(poll_seconds)


def _parse_tme_link(url: str) -> dict | None:
    value = url.strip()
    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = "https://" + value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    if host not in _TME_HOSTS:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) == 3 and parts[0].lower() == "c":
        channel_id, msg_id = parts[1], parts[2]
        if not channel_id.isdigit() or not msg_id.isdigit() or int(channel_id) <= 0:
            return None
        peer = int("-100" + channel_id)
    elif len(parts) == 3 and parts[0].lower() == "s":
        username, msg_id = parts[1], parts[2]
        if not re.fullmatch(r"[A-Za-z0-9_]{3,}", username) or not msg_id.isdigit():
            return None
        peer = username.casefold()
    elif len(parts) == 2:
        username, msg_id = parts
        if not re.fullmatch(r"[A-Za-z0-9_]{3,}", username) or not msg_id.isdigit():
            return None
        peer = username.casefold()
    else:
        return None

    message_id = int(msg_id)
    if message_id <= 0:
        return None
    return {"url": url, "msg_id": message_id, "peer": peer}


def extract_tme_links(text: str) -> list[dict]:
    """Return a list of unique Telegram message links found in *text*."""
    if not text:
        return []
    results = []
    seen = set()
    for pat in TME_PATTERNS:
        for match in pat.finditer(text):
            info = _parse_tme_link(match.group("url"))
            if info is None:
                continue
            key = (str(info["peer"]).casefold(), info["msg_id"])
            if key in seen:
                continue
            seen.add(key)
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
