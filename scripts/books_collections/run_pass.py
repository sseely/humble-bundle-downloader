# pyright: basic
"""Add every book in plan.json to its collections through the Books UI.

Creates missing collections, then walks the library list top to bottom and, for
each book with pending adds, uses right-click > Add to Collection. Every add is
recorded in progress.json at once, so the run can stop and resume at any time.
It stops on any unexpected state: Books not frontmost, a sheet that will not
open or close, or a collection count that does not match.
"""

import time

import store
from AppKit import NSPasteboard, NSPasteboardItem, NSPasteboardTypeString
from ax import Stop, post_key, press, require_front
from booksui import (
    cancel_menus,
    close_sheet,
    live_labels,
    new_collection_button,
    open_add_sheet,
    scroll,
    scroll_to_top,
    sheet,
    sheet_find,
    sheet_scan,
    show_all,
    visible_button,
)
from Quartz import kCGEventFlagMaskCommand

from humble_dl.services.books_collections import strip_progress

RETURN_KEY = 36
V_KEY = 9


def log(*parts: object) -> None:
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def paste_and_return(text: str) -> None:
    """Paste ``text`` into Books, then Return; typing gets autocorrected (' -> ’)."""
    board = NSPasteboard.generalPasteboard()
    saved = [{t: it.dataForType_(t) for t in it.types()} for it in board.pasteboardItems() or []]
    try:
        board.clearContents()
        board.setString_forType_(text, NSPasteboardTypeString)
        post_key(V_KEY, kCGEventFlagMaskCommand)
        time.sleep(0.5)
        post_key(RETURN_KEY)
        time.sleep(0.3)
    finally:
        board.clearContents()
        items = []
        for entry in saved:
            item = NSPasteboardItem.alloc().init()
            for kind, data in entry.items():
                item.setData_forType_(data, kind)
            items.append(item)
        if items:
            board.writeObjects_(items)


def create_collection(name: str) -> None:
    button = new_collection_button()
    press(button, "AXScrollToVisible")
    time.sleep(0.3)
    require_front()
    press(button)
    time.sleep(1.0)
    require_front()
    paste_and_return(name)
    time.sleep(1.5)
    if name not in sheet_scan():
        raise Stop(f"collection not created: {name}")
    log("created collection:", name)


def open_sheet_for(label: str) -> bool:
    button = visible_button(label)
    if button is None:
        return False
    require_front()
    try:
        open_add_sheet(button)
    except Stop as err:
        log("could not open the sheet for", label, "|", err)
        cancel_menus()
        return False
    return True


class Run:
    def __init__(self, plan: dict[str, list[str]], done: dict[str, list[str]]) -> None:
        self.plan = plan
        self.done = done
        self.expected: dict[str, int] = {}  # counts the sheet must show
        self.failed: list[str] = []

    def pending(self, label: str) -> list[str]:
        finished = self.done.get(strip_progress(label), [])
        return [c for c in self.plan.get(label, []) if c not in finished]

    def check_counts(self, counts: dict[str, int]) -> None:
        for name, count in counts.items():
            want = self.expected.setdefault(name, count)
            if count != want:
                raise Stop(f"count mismatch for {name}: sheet {count}, expected {want}")

    def add(self, label: str, target: str) -> None:
        self.check_counts(sheet_scan())
        row = sheet_find(target)
        press(row, "AXScrollToVisible")
        time.sleep(0.2)
        require_front()
        press(row)
        time.sleep(1.2)
        if sheet() is not None:
            raise Stop(f"sheet still open after adding {label!r} to {target}")
        self.expected[target] += 1
        self.done.setdefault(strip_progress(label), []).append(target)
        store.save(store.PROGRESS_FILE, self.done)

    def process(self, label: str) -> None:
        for target in self.pending(label):
            if not open_sheet_for(label):
                self.failed.append(label)
                return
            self.add(label, target)
            log("added", label[:60], "->", target[:60])

    def ensure_collections(self, label: str) -> None:
        wanted = sorted({c for cs in self.plan.values() for c in cs})
        if not open_sheet_for(label):
            raise Stop("could not open a sheet to create collections")
        existing = sheet_scan()
        self.check_counts(existing)  # baseline: what Books shows now
        for name in wanted:
            if name not in existing:
                create_collection(name)
        self.check_counts(sheet_scan())
        close_sheet()

    def todo(self) -> list[str]:
        return [lab for lab in live_labels() if self.pending(lab) and lab not in self.failed]

    def sweep(self) -> None:
        show_all()  # a collection may be selected; the plan covers the whole library
        scroll_to_top()
        first = next((lab for lab in live_labels() if lab in self.plan), None)
        if first is None:
            raise Stop("no planned book is visible at the top of the library")
        self.ensure_collections(first)
        while True:
            while todo := self.todo():  # re-read: scrolling a row shifts the others
                self.process(todo[0])
            before = live_labels()
            scroll("down")
            if live_labels() == before:
                return


def main() -> int:
    plan: dict[str, list[str]] | None = store.load(store.PLAN_FILE)
    if plan is None:
        print(f"No {store.PLAN_FILE} in {store.STATE_DIR}; run plan.py first")
        return 1
    run = Run(plan, store.load(store.PROGRESS_FILE, {}))
    if not any(run.pending(lab) for lab in plan):
        log("nothing to add")
        return 0
    try:
        require_front()
        close_sheet()  # left open by an interrupted run
        run.sweep()
    except Stop as err:
        log("STOPPED:", err)
        return 2
    left = {lab: run.pending(lab) for lab in plan if run.pending(lab)}
    store.save(store.LEFTOVER_FILE, left)
    log(f"done. pending: {len(left)} books | could not open: {run.failed}")
    return 0 if not left else 3


if __name__ == "__main__":
    raise SystemExit(main())
