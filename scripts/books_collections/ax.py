# pyright: basic
# pyobjc ships no type stubs; the gated pyright run covers src/ and tests/ only.
"""macOS accessibility (AX) primitives for driving Apple Books.

Reads the UI tree and performs AX actions on Books only; key events are posted
to the Books process, never to the system. Requires the Accessibility
permission for the terminal running the script.
"""

import time
from collections.abc import Callable
from typing import Any

from AppKit import NSWorkspace
from ApplicationServices import (
    AXUIElementCopyActionNames,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementPerformAction,
)
from Quartz import CGEventCreateKeyboardEvent, CGEventPostToPid, CGEventSetFlags

BOOKS_ID = "com.apple.iBooksX"
AX_OK = 0
LIBRARY_ACTIONS = "Library Actions"  # toolbar button only the library window has

Element = Any  # an AXUIElementRef


class Stop(Exception):
    """The UI is not in the expected state; the run stops with progress saved."""


def books_pid() -> int:
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() == BOOKS_ID:
            return app.processIdentifier()
    raise Stop("Books is not running")


def frontmost_id() -> str:
    return NSWorkspace.sharedWorkspace().frontmostApplication().bundleIdentifier()


def require_front() -> None:
    """Books ignores AX presses unless it is frontmost; never act otherwise."""
    front = frontmost_id()
    if front != BOOKS_ID:
        raise Stop(f"Books is not frontmost ({front}); bring it to the front and re-run")


def attr(el: Element, name: str) -> Any:
    err, val = AXUIElementCopyAttributeValue(el, name, None)
    return val if err == AX_OK else None


def actions(el: Element) -> list[str]:
    err, names = AXUIElementCopyActionNames(el, None)
    return list(names) if err == AX_OK and names else []


def press(el: Element, action: str = "AXPress") -> int:
    return AXUIElementPerformAction(el, action)


def walk(el: Element, max_depth: int = 40, limit: int = 4000) -> list[Element]:
    """Elements under ``el`` in depth-first order, at most ``limit`` of them."""
    out: list[Element] = []
    stack: list[tuple[Element, int]] = [(el, 0)]
    while stack and len(out) < limit:
        node, depth = stack.pop()
        out.append(node)
        if depth < max_depth:
            kids = attr(node, "AXChildren") or []
            stack.extend((k, depth + 1) for k in reversed(kids))
    return out


def find(
    pred: Callable[[Element], bool], root: Element, limit: int = 3000, max_depth: int = 40
) -> Element | None:
    return next((e for e in walk(root, max_depth, limit) if pred(e)), None)


def app() -> Element:
    return AXUIElementCreateApplication(books_pid())


def _is_library(win: Element) -> bool:
    return any(attr(e, "AXDescription") == LIBRARY_ACTIONS for e in walk(win, 3, 60))


def window() -> Element:
    """The Books library window (reader windows are skipped)."""
    for win in attr(app(), "AXWindows") or []:
        if _is_library(win):
            return win
    raise Stop("Books library window not found; open it with Window > Library")


def wait_for(pred: Callable[[], Any], timeout: float = 5.0, step: float = 0.2) -> Any:
    """Poll ``pred`` until it returns something other than None, or time out."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        found = pred()
        if found is not None:
            return found
        time.sleep(step)
    return None


def post_key(code: int, flags: int = 0) -> None:
    """Send one key press to the Books process only."""
    pid = books_pid()
    for down in (True, False):
        event = CGEventCreateKeyboardEvent(None, code, down)
        if flags:
            CGEventSetFlags(event, flags)
        CGEventPostToPid(pid, event)
