"""Interactive dialog picker: live search + arrow keys + Enter.

Type to filter the dialog list, move with Up/Down, confirm with Enter,
cancel with Esc. Ctrl+C also aborts (handled by the caller).
"""

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
    parts = [
        str(getattr(dialog, "name", "") or ""),
        str(getattr(entity, "username", "") or ""),
        str(getattr(entity, "phone", "") or ""),
        str(getattr(entity, "id", "") or ""),
    ]
    return " ".join(parts).lower()


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


async def pick_dialog(dialogs):
    """Show the picker; return the chosen Dialog, or None if aborted."""
    if not dialogs:
        print("[WARN] No dialogs found on this account.")
        return None

    entries = [
        {"dialog": d, "title": dialog_title(d), "search": dialog_search_text(d)}
        for d in dialogs
    ]
    state = {"index": 0, "result": None, "aborted": False}
    holder: dict = {}

    def visible() -> list[dict]:
        items = filter_entries(entries, search_field.text)
        if state["index"] >= len(items):
            state["index"] = max(0, len(items) - 1)
        return items

    def accept(_buffer) -> None:
        items = visible()
        if items and 0 <= state["index"] < len(items):
            state["result"] = items[state["index"]]["dialog"]
        else:
            state["aborted"] = True
        app = holder.get("app")
        if app is not None:
            app.exit()

    search_field = TextArea(
        multiline=False,
        height=1,
        prompt="Search dialogs: ",
        accept_handler=accept,
    )
    # Keep the highlight on the first match while typing.
    search_field.buffer.on_text_changed.add_handler(lambda _: state.update(index=0))

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

    footer = Window(
        content=FormattedTextControl(
            [("", "Type to filter  •  ↑/↓ select  •  Enter confirm  •  Esc cancel")]
        ),
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
        }
    )

    app = Application(
        layout=Layout(HSplit([search_field, Window(FormattedTextControl(get_list_tokens)), footer])),
        key_bindings=kb,
        style=style,
        full_screen=False,
    )
    holder["app"] = app
    print(f"[PICK] {len(entries)} dialogs — type to search, ↑/↓ to move, Enter to confirm.")
    await app.run_async()

    if state["aborted"] or state["result"] is None:
        return None
    return state["result"]
