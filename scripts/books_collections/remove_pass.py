# pyright: basic
"""Delete the duplicate copies listed in remove.json from Books (and iCloud).

For each copy, right-click > Remove… opens Books' alert; the script presses
Delete Everywhere and answers the "Delete from iCloud?" confirmation with
Delete, which deletes that copy from iCloud on every device, then checks that
exactly that entry left the library list. The kept copy is never
touched. Every removal is recorded in removed.json at once, so the run can stop
and resume at any time. It stops on any unexpected state.
"""

import sys
import time

import store
from ax import Stop, press, require_front
from booksui import (
    DELETE_EVERYWHERE,
    alert_button,
    alert_texts,
    close_sheet,
    live_labels,
    open_remove_alert,
    scroll,
    scroll_to_top,
    sheet,
    show_all,
    visible_button,
)
from run_pass import log

from humble_dl.services.book_info import removal_targets
from humble_dl.services.books_collections import strip_progress

GONE_WAIT_S = 5.0
CANCEL = "Cancel"
CONFIRM_TITLE = "Delete from iCloud?"  # second alert after Delete Everywhere
CONFIRM_DELETE = "Delete"


class Remove:
    def __init__(self, targets: dict[str, str], removed: list[str], limit: int | None) -> None:
        self.targets = targets
        self.removed = removed
        self.failed: list[str] = []
        self.left = limit  # removals still allowed this run (None: no limit)

    def todo(self) -> list[str]:
        if self.left == 0:
            return []
        done = set(self.removed)
        return [
            lab
            for lab in live_labels()
            if strip_progress(lab) in self.targets
            and strip_progress(lab) not in done
            and lab not in self.failed
        ]

    def gone(self, key: str) -> bool:
        end = time.monotonic() + GONE_WAIT_S
        while time.monotonic() < end:
            if all(strip_progress(lab) != key for lab in live_labels()):
                return True
            time.sleep(0.3)
        return False

    def apply(self, label: str) -> None:
        key = strip_progress(label)
        button = visible_button(label)
        if button is None:
            self.failed.append(label)
            return
        require_front()
        alert = open_remove_alert(button)
        try:
            delete = alert_button(alert, DELETE_EVERYWHERE)
        except Stop:  # an alert worded differently: leave this copy alone
            press(alert_button(alert, CANCEL))
            self.failed.append(label)
            return
        require_front()
        press(delete)
        time.sleep(1.0)
        self.confirm(label)
        if sheet() is not None:
            raise Stop(f"a sheet is still open after removing {label!r}")
        if not self.gone(key):
            raise Stop(f"{label!r} is still in the library after Delete Everywhere")
        self.removed.append(key)
        store.save(store.REMOVED_FILE, self.removed)
        self.left = None if self.left is None else self.left - 1
        log("removed", key[:60], "| kept:", self.targets[key][:60])

    def confirm(self, label: str) -> None:
        """Books asks "Delete from iCloud?" after Delete Everywhere; answer Delete."""
        alert = sheet()
        if alert is None:
            return
        texts = alert_texts(alert)
        if CONFIRM_TITLE not in texts:
            raise Stop(f"unexpected alert after Delete Everywhere for {label!r}: {texts}")
        require_front()
        press(alert_button(alert, CONFIRM_DELETE))
        time.sleep(1.0)

    def sweep(self) -> None:
        show_all()
        scroll_to_top()
        while True:
            while todo := self.todo():  # re-read: removing a row shifts the others
                self.apply(todo[0])
            if self.left == 0:
                return
            before = live_labels()
            scroll("down")
            if live_labels() == before:
                return


def main() -> int:
    remove = store.load(store.REMOVE_FILE)
    if remove is None:
        print(f"No {store.REMOVE_FILE} in {store.STATE_DIR}; run info.py first")
        return 1
    removed: list[str] = store.load(store.REMOVED_FILE, [])
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None  # optional: remove at most N
    run = Remove(removal_targets(remove, removed), removed, limit)
    if not run.targets:
        log("nothing to remove")
        return 0
    try:
        require_front()
        close_sheet()  # left open by an interrupted run
        run.sweep()
    except Stop as err:
        log("STOPPED:", err)
        return 2
    left = [key for key in run.targets if key not in set(run.removed)]
    log(f"done. removed: {len(run.targets) - len(left)} | not removed: {len(left)}")
    for label in run.failed:
        log("  skipped (not visible, or alert without Delete Everywhere):", label)
    for key in left:
        log("  not removed:", key)
    return 0 if not left else 3


if __name__ == "__main__":
    raise SystemExit(main())
