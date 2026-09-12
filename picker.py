"""Interactive dialog picker: live search + arrow keys + Enter.

Type to filter the dialog list, move with Up/Down, confirm with Enter,
cancel with Esc. Ctrl+C also aborts (handled by the caller).

If the wanted chat is not in the dialog list (e.g. a public channel never
joined), typing its @username / ID / t.me link offers a "open directly"
row that resolves it via the API instead.
"""

import re
import asyncio

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from telethon.tl.types import Channel, Chat, User

MAX_VISIBLE = 20


def dialog_tag(dialog) -> str:
    """Short type tag for a Telethon Dialog."""
    entity = getattr(dialog, "entity", None)
    if getattr(entity, "is_self", False):
        return "ME"
    if isinstance(entity, User):
        return "DM"
    if isinstance(entity, Channel):
        return "CHAN" if getattr(entity, "broadcast", False) else "GROUP"
    if isinstance(entity, Chat):
        return "GROUP"
    return "?"


def dialog_title(dialog) -> str:
    """One-line display string for a dialog."""
    name = getattr(dialog, "name", None) or "(no name)"
    entity = getattr(dialog, "entity", None)
    username = getattr(entity, "username", None)
    sub = f"@{username}" if username else f"id {getattr(entity, 'id', '?')}"
    unread = getattr(dialog, "unread_count", 0) or 0
    extra = f" • {unread} unread" if unread else ""
    return f"[{dialog_tag(dialog)}] {name} ({sub}){extra}"


def dialog_search_text(dialog) -> str:
    """Lowercased haystack for substring filtering."""
    entity = getattr(dialog, "entity", None) or object()
    username = str(getattr(entity, "username", "") or "")
    parts = [
        str(getattr(dialog, "name", "") or ""),
        username,
        f"@{username}" if username else "",
        str(getattr(entity, "phone", "") or ""),
        str(getattr(entity, "id", "") or ""),
    ]
    return " ".join(parts).lower()


def normalize_query(query: str) -> str:
    """Normalise raw picker input: strip @, unwrap pasted t.me links."""
    q = (query or "").strip()
    if "t.me/" in q.lower():
        q = q.lower().split("t.me/")[-1]
    q = q.strip().lstrip("@").strip().strip("/")
    return q.split("?")[0].strip()


def direct_candidate(query: str) -> str | int | None:
    """If the query looks like a directly-openable peer, return it.

    Returns a username, a numeric ID, or a -100… channel ID for t.me/c/
    links. Returns None for invite links (t.me/+hash needs joining first)
    and for anything that is clearly just a name fragment.
    """
    q = normalize_query(query)
    if not q or q.startswith("+"):
        return None
    if q.startswith("c/"):  # t.me/c/<chatid>[/<msgid>]
        parts = q.split("/")
        if len(parts) >= 2 and parts[1].isdigit():
            return int("-100" + parts[1])
        return None
    seg = q.split("/")[0].strip()
    if seg.isdigit():
        return int(seg)
    if re.fullmatch(r"[A-Za-z0-9_]{3,}", seg):
        return seg
    return None


def filter_entries(entries: list[dict], query: str) -> list[dict]:
    """Return entries whose search text contains *query* (case-insensitive)."""
    q = (query or "").strip().lower()
    if not q:
        return entries
    return [e for e in entries if q in e["search"]]


def entity_name(entity) -> str:
    """Human-readable name for a bare entity (used with --peer)."""
    if entity == "me" or getattr(entity, "is_self", False):
        return "Saved Messages"
    title = getattr(entity, "title", None)
    if title:
        return title
    full = f"{getattr(entity, 'first_name', '') or ''} {getattr(entity, 'last_name', '') or ''}".strip()
    if full:
        return full
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    return f"id {getattr(entity, 'id', '?')}"


def format_dialog_line(index: int, dialog, peer_key: str = "", source: str = "") -> str:
    """Numbered one-line dump of a dialog (for --debug-dialogs output)."""
    unread = getattr(dialog, "unread_count", 0) or 0
    extra = []
    if peer_key:
        extra.append(f"key={peer_key}")
    if source:
        extra.append(f"src={source}")
    if unread:
        extra.append(f"unread={unread}")
    suffix = f" [{', '.join(extra)}]" if extra else ""
    return f"{index}. {dialog_title(dialog)}{suffix}"


async def pick_dialog(dialogs, resolve=None):
    """Show the picker; return the chosen Dialog (or entity namespace), or None.

    *resolve* is an optional ``async (username_or_id) -> entity`` callable.
    When provided and the typed query looks like a directly-openable peer,
    an extra "open directly" row appears so chats missing from the dialog
    list (e.g. public channels never joined) can still be archived.
    """
    if not dialogs:
        print("[WARN] No dialogs found on this account.")
        return None

    from types import SimpleNamespace

    entries = [
        {"dialog": d, "title": dialog_title(d), "search": dialog_search_text(d)}
        for d in dialogs
    ]
    state = {"index": 0, "result": None, "aborted": False, "retry": False, "error": ""}
    holder: dict = {}

    def visible() -> list[dict]:
        items = filter_entries(entries, normalize_query(search_field.text))
        cand = direct_candidate(search_field.text) if resolve else None
        if cand is not None:
            items = list(items) + [{
                "dialog": None,
                "title": f"⟶  直接打开 '{cand}'（不在会话列表中）",
                "search": "",
                "direct": cand,
            }]
        if state["index"] >= len(items):
            state["index"] = max(0, len(items) - 1)
        return items

    async def accept_entry(entry: dict) -> bool:
        """Store the result; return True if the picker should exit."""
        if "direct" in entry:
            try:
                entity = await resolve(entry["direct"])
            except Exception as exc:
                state["error"] = f"无法打开 '{entry['direct']}': {exc}"
                state["retry"] = True
                return False
            state["result"] = SimpleNamespace(entity=entity, name=entity_name(entity))
            return True
        state["result"] = entry["dialog"]
        return True

    def accept(_buffer) -> None:
        items = visible()
        if not items or not (0 <= state["index"] < len(items)):
            state["aborted"] = True
            holder["app"].exit()
            return
        # accept_entry is async — schedule it; it exits the app when done
        # (the UI stays up while a direct-open resolve is in flight).
        asyncio.ensure_future(_finish_accept(items[state["index"]]))

    async def _finish_accept(entry: dict) -> None:
        done = await accept_entry(entry)
        if done or state["retry"]:
            holder["app"].exit()

    search_field = TextArea(
        multiline=False,
        height=1,
        prompt="Search dialogs: ",
        accept_handler=accept,
    )

    def _on_type_changed(_) -> None:
        # Keep the highlight on the first match while typing.
        state.update(index=0, error="")

    search_field.buffer.on_text_changed.add_handler(_on_type_changed)

    def get_list_tokens():
        items = visible()
        if not items:
            return [("", "(no matches — keep typing, or Esc to cancel)")]
        tokens = []
        for i, entry in enumerate(items[:MAX_VISIBLE]):
            if i == state["index"]:
                tokens.append(("class:selected", f"▶ {entry['title']}\n"))
            else:
                tokens.append(("", f"  {entry['title']}\n"))
        if len(items) > MAX_VISIBLE:
            tokens.append(("", f"  … and {len(items) - MAX_VISIBLE} more (keep typing to narrow)\n"))
        return tokens

    def get_footer_tokens():
        if state["error"]:
            return [("class:error", f"(!) {state['error']}")]
        return [("", "Type to filter  •  ↑/↓ select  •  Enter confirm  •  Esc cancel")]

    footer = Window(
        content=FormattedTextControl(get_footer_tokens),
        height=1,
        style="class:footer",
    )

    kb = KeyBindings()

    @kb.add("up")
    def _go_up(event):
        state["index"] = max(0, state["index"] - 1)

    @kb.add("down")
    def _go_down(event):
        state["index"] = min(max(0, len(visible()) - 1), state["index"] + 1)

    @kb.add("escape")
    def _abort(event):
        state["aborted"] = True
        event.app.exit()

    style = Style.from_dict(
        {
            "selected": "reverse",
            "footer": "fg:ansibrightblack",
            "error": "fg:ansired",
        }
    )

    print(f"[PICK] {len(entries)} dialogs — type to search, ↑/↓ to move, Enter to confirm.")
    # A fresh Application per run: a failed direct-open resolve exits the
    # current run to show the error, then the UI is rebuilt with the query kept.
    while True:
        state.update(result=None, aborted=False, retry=False)
        app = Application(
            layout=Layout(
                HSplit([search_field, Window(FormattedTextControl(get_list_tokens)), footer]),
                focused_element=search_field,
            ),
            key_bindings=kb,
            style=style,
            full_screen=False,
        )
        holder["app"] = app
        await app.run_async()
        if not state["retry"]:
            break

    if state["aborted"] or state["result"] is None:
        return None
    return state["result"]
