# pyright: basic
"""Retitle Books entries and set their authors from book_info.json.

For each entry, right-click > Rename opens Books' Book Info sheet; the script
sets the Title and Author fields through accessibility and presses OK, then
checks that the new label appears. Every rename is recorded in renamed.json at
once, and the book's collections progress moves to its new label, so the run
can stop and resume at any time and books-collections.sh will not re-add it.
Books also renames the book's file in its iCloud folder to the new title.
"""

import time

import store
from ApplicationServices import AXUIElementSetAttributeValue
from ax import AX_OK, Stop, attr, press, require_front
from booksui import (
    cancel_menus,
    close_sheet,
    info_controls,
    live_labels,
    open_info_sheet,
    scroll,
    scroll_to_top,
    sheet,
    show_all,
    visible_button,
)
from run_pass import log

from humble_dl.services.book_info import move_progress, new_key, rename_targets
from humble_dl.services.books_collections import is_cloud_only, strip_progress

LABEL_WAIT_S = 5.0


def set_value(field, text: str) -> None:
    err = AXUIElementSetAttributeValue(field, "AXValue", text)
    if err != AX_OK or attr(field, "AXValue") != text:
        raise Stop(f"could not set a Book Info field to {text!r} (AX error {err})")


class Rename:
    def __init__(self, targets: dict[str, tuple[str, str]]) -> None:
        self.targets = targets
        self.renamed: dict[str, str] = store.load(store.RENAMED_FILE, {})
        self.failed: list[str] = []

    def todo(self) -> list[str]:
        return [
            lab
            for lab in live_labels()
            if strip_progress(lab) in self.targets
            and strip_progress(lab) not in self.renamed
            and lab not in self.failed
        ]

    def wait_for_label(self, key: str) -> bool:
        end = time.monotonic() + LABEL_WAIT_S
        while time.monotonic() < end:
            if any(strip_progress(lab) == key for lab in live_labels()):
                return True
            time.sleep(0.3)
        return False

    def apply(self, label: str) -> None:
        old = strip_progress(label)
        title, author = self.targets[old]
        button = visible_button(label) if not is_cloud_only(label) else None
        if button is None:
            self.failed.append(label)
            return
        require_front()
        if not open_info_sheet(button):  # no Rename: not downloaded after all
            self.failed.append(label)
            return
        title_field, author_field, ok = info_controls()
        set_value(title_field, title)
        set_value(author_field, author)
        require_front()
        press(ok)
        time.sleep(1.0)
        if sheet() is not None:
            raise Stop(f"Book Info sheet still open after renaming {label!r}")
        self.record(old, new_key(title, author), label)

    def record(self, old: str, new: str, label: str) -> None:
        self.renamed[old] = new
        store.save(store.RENAMED_FILE, self.renamed)
        done = store.load(store.PROGRESS_FILE, {})
        store.save(store.PROGRESS_FILE, move_progress(done, old, new))
        found = self.wait_for_label(new)
        log("renamed" if found else "renamed (new label not visible yet)", old[:50], "->", new[:70])

    def sweep(self) -> None:
        show_all()  # a collection may be selected; renames work from the whole library
        scroll_to_top()
        while True:
            while todo := self.todo():  # re-read: a renamed row moves to its new place
                try:
                    self.apply(todo[0])
                except Stop:
                    cancel_menus()
                    raise
            before = live_labels()
            scroll("down")
            if live_labels() == before:
                return


def main() -> int:
    entries = store.load(store.BOOK_INFO_FILE)
    if entries is None:
        print(f"No {store.BOOK_INFO_FILE} in {store.STATE_DIR}; run info.py first")
        return 1
    run = Rename(rename_targets(entries, store.load(store.RENAMED_FILE, {})))
    if not run.targets:
        log("nothing to rename")
        return 0
    try:
        require_front()
        close_sheet()  # left open by an interrupted run
        run.sweep()
    except Stop as err:
        log("STOPPED:", err)
        return 2
    left = [key for key in run.targets if key not in run.renamed]
    log(f"done. renamed: {len(run.targets) - len(left)} | not renamed: {len(left)}")
    for label in run.failed:
        log("  in iCloud only or no Rename item; download it in Books, then re-run:", label)
    for key in left:
        log("  not renamed:", key)
    return 0 if not left else 3


if __name__ == "__main__":
    raise SystemExit(main())
