# pyright: basic
"""Apple Books library list and Add to Collection sheet, via accessibility only."""

import re
import time

from ApplicationServices import AXValueGetValue, kAXValueCGSizeType
from ax import Element, Stop, actions, attr, find, press, wait_for, walk, window

LIBRARY_ROWS = {
    "Want to Read",
    "Finished",
    "Books",
    "Audiobooks",
    "PDFs",
    "My Samples",
    "Downloaded",
}
NEW_COLLECTION = "New Collection"
CLOSE_BUTTONS = {"close", "Cancel"}
SCROLL_DOWN = "AXScrollDownByPage"
SCROLL_UP = "AXScrollUpByPage"
_COUNT = re.compile(r"^(.*?)(?:, (\d+))?$")  # "Name, 12" (count hidden while editing)


# --- library list ---------------------------------------------------------

ALL_VIEW = "All"  # sidebar row for the whole library


def show_all() -> None:
    """Select the sidebar's All view (the scripts read the whole library list)."""
    sidebar = find(lambda e: attr(e, "AXDescription") == "Sidebar", window())
    if sidebar is None:
        raise Stop("Books sidebar not found; show it with View > Show Sidebar")
    for _ in range(10):
        if press(sidebar, SCROLL_UP) != 0:
            break
        time.sleep(0.2)
    row = find(
        lambda e: attr(e, "AXRole") == "AXStaticText" and attr(e, "AXDescription") == ALL_VIEW,
        sidebar,
        limit=200,
    )
    if row is None:
        raise Stop("All view not found in the Books sidebar")
    press(row)
    if wait_for(lambda: library_group()) is None:
        raise Stop("Books library list not visible after selecting All")


def library_group() -> Element | None:
    return find(
        lambda e: attr(e, "AXRole") == "AXGroup" and attr(e, "AXDescription") == "Library",
        window(),
    )


def book_buttons() -> list[Element]:
    group = library_group()
    kids = (attr(group, "AXChildren") or []) if group is not None else []
    return [k for k in kids if attr(k, "AXRole") == "AXButton"]


def labels() -> list[str]:
    return [attr(b, "AXDescription") for b in book_buttons()]


def live_labels() -> list[str]:
    """Visible labels; the list is briefly unexposed right after a sheet closes."""
    found = wait_for(lambda: labels() or None)
    if not found:
        raise Stop("Books library list not visible")
    return found


def scroll(direction: str) -> None:
    btns = wait_for(lambda: book_buttons() or None)
    if not btns:
        raise Stop("Books library list not visible")
    target, action = (btns[-1], SCROLL_DOWN) if direction == "down" else (btns[0], SCROLL_UP)
    press(target, action)
    time.sleep(0.4)


def scroll_to_top() -> None:
    for _ in range(80):
        before = labels()
        scroll("up")
        if labels() == before:
            return


def height(el: Element) -> float:
    ok, size = AXValueGetValue(attr(el, "AXSize"), kAXValueCGSizeType, None)
    return size.height if ok else 0.0


def visible_button(label: str) -> Element | None:
    """The fully visible row for ``label``; a partly hidden row opens no menu."""
    for _ in range(2):
        btns = book_buttons()
        full = max((height(b) for b in btns), default=0.0)
        for b in btns:
            if attr(b, "AXDescription") == label:
                if height(b) >= full * 0.95:
                    return b
                press(b, "AXScrollToVisible")
                time.sleep(0.5)
    return None


def cancel_menus() -> None:
    for e in walk(window(), limit=8000):
        if attr(e, "AXRole") == "AXMenu":
            press(e, "AXCancel")


# --- Add to Collection sheet ----------------------------------------------


def sheet() -> Element | None:
    return find(lambda e: attr(e, "AXRole") == "AXSheet", window(), limit=50, max_depth=2)


def open_add_sheet(button: Element) -> None:
    press(button, "AXShowMenu")
    item = wait_for(
        lambda: find(lambda e: attr(e, "AXIdentifier") == "addToCollection", window(), 8000)
    )
    if item is None:
        raise Stop("Add to Collection menu item not found")
    press(item)
    if wait_for(sheet) is None:
        raise Stop("Add to Collection sheet did not open")
    time.sleep(0.3)


def sheet_page() -> tuple[list[tuple[str, int, Element]], Element | None]:
    """Collection rows the lazy sheet list exposes now, and the New Collection button."""
    sh = sheet()
    if sh is None:
        raise Stop("Add to Collection sheet not open")
    rows: list[tuple[str, int, Element]] = []
    new_btn = None
    for e in walk(sh, limit=500):
        role, desc = attr(e, "AXRole"), attr(e, "AXDescription")
        if role == "AXButton" and desc and NEW_COLLECTION in desc:
            new_btn = e
        elif role == "AXStaticText" and desc:
            m = _COUNT.match(desc)
            if m:
                rows.append((m.group(1), int(m.group(2) or 0), e))
    return rows, new_btn


def _sheet_scroll(direction: str) -> bool:
    """Page the sheet list; False when it cannot move that way."""
    rows, _ = sheet_page()
    if not rows:
        return False
    before = [r[0] for r in rows]
    target, action = (rows[-1][2], SCROLL_DOWN) if direction == "down" else (rows[0][2], SCROLL_UP)
    if action not in actions(target):
        return False
    press(target, action)
    time.sleep(0.35)
    return [r[0] for r in sheet_page()[0]] != before


def sheet_pages():
    """Yield each page of the sheet, top to bottom."""
    for _ in range(10):
        if not _sheet_scroll("up"):
            break
    for _ in range(20):
        yield sheet_page()
        if not _sheet_scroll("down"):
            return


def sheet_scan() -> dict[str, int]:
    """Every user collection and its book count."""
    cols: dict[str, int] = {}
    for rows, _ in sheet_pages():
        cols.update({n: c for n, c, _ in rows if n not in LIBRARY_ROWS})
    return cols


def sheet_find(name: str) -> Element:
    for rows, _ in sheet_pages():
        for n, _, e in rows:
            if n == name:
                return e
    raise Stop(f"collection row not found in sheet: {name}")


def new_collection_button() -> Element:
    btn = None
    for _, b in sheet_pages():
        btn = b or btn
    if btn is None:
        raise Stop("New Collection… button not found")
    return btn


def close_sheet() -> None:
    sh = sheet()
    if sh is None:
        return
    # Add to Collection has a close button; Book Info (rename) has Cancel.
    close = [e for e in walk(sh, limit=500) if attr(e, "AXDescription") in CLOSE_BUTTONS]
    if not close:
        raise Stop("sheet close button not found")
    press(close[0])
    time.sleep(0.8)
    if sheet() is not None:
        raise Stop("sheet did not close")


# --- Book Info sheet (right-click > Rename) -------------------------------


RENAME_ITEM = "rename"  # context-menu AXIdentifier; opens the Book Info sheet
OK_BUTTON = "OK"


def open_info_sheet(button: Element) -> bool:
    """Right-click > Rename: a sheet with Title and Author fields, Cancel and OK.

    False (menu closed again) when the menu has no Rename item: Books shows it
    only for downloaded books.
    """
    press(button, "AXShowMenu")
    item = wait_for(lambda: find(lambda e: attr(e, "AXIdentifier") == RENAME_ITEM, window(), 8000))
    if item is None:
        cancel_menus()
        return False
    press(item)
    if wait_for(sheet) is None:
        raise Stop("Book Info sheet did not open")
    time.sleep(0.3)
    return True


def info_controls() -> tuple[Element, Element, Element]:
    """The open Book Info sheet's Title field, Author field and OK button."""
    sh = sheet()
    if sh is None:
        raise Stop("Book Info sheet not open")
    elements = walk(sh, limit=300)
    fields = [e for e in elements if attr(e, "AXRole") == "AXTextField"]
    ok = [
        e
        for e in elements
        if attr(e, "AXRole") == "AXButton" and attr(e, "AXDescription") == OK_BUTTON
    ]
    if len(fields) != 2 or len(ok) != 1:
        raise Stop(f"unexpected Book Info sheet: {len(fields)} fields, {len(ok)} OK buttons")
    return fields[0], fields[1], ok[0]


# --- Remove alert (right-click > Remove…) ---------------------------------


REMOVE_ITEM = "remove"  # context-menu AXIdentifier; opens an alert sheet
DELETE_EVERYWHERE = "Delete Everywhere"  # deletes the copy from iCloud on every device


def open_remove_alert(button: Element) -> Element:
    """Right-click > Remove…: the alert sheet (Remove Download / Delete Everywhere / Cancel)."""
    press(button, "AXShowMenu")
    item = wait_for(lambda: find(lambda e: attr(e, "AXIdentifier") == REMOVE_ITEM, window(), 8000))
    if item is None:
        cancel_menus()
        raise Stop("Remove menu item not found")
    press(item)
    alert = wait_for(sheet)
    if alert is None:
        raise Stop("Remove alert did not open")
    time.sleep(0.3)
    return alert


def alert_button(alert: Element, title: str) -> Element:
    found = [
        e
        for e in walk(alert, limit=300)
        if attr(e, "AXRole") == "AXButton" and attr(e, "AXTitle") == title
    ]
    if len(found) != 1:
        raise Stop(f"Remove alert has no single {title!r} button")
    return found[0]


def alert_texts(alert: Element) -> list[str]:
    """The static texts of an alert sheet (title and message)."""
    return [
        attr(e, "AXValue")
        for e in walk(alert, limit=300)
        if attr(e, "AXRole") == "AXStaticText" and attr(e, "AXValue")
    ]
