# pyright: basic
"""Record every label in the Books library list (the All view) in library_items.json."""

import store
from ax import Stop, require_front
from booksui import close_sheet, live_labels, scroll, scroll_to_top, show_all

STALL_PAGES = 3  # pages in a row with no new label before the bottom is assumed


def scan() -> list[str]:
    close_sheet()
    show_all()  # a collection may be selected; the scan reads the whole library
    scroll_to_top()
    seen: set[str] = set()
    order: list[str] = []
    stall = 0
    while stall < STALL_PAGES:
        new = [label for label in live_labels() if label not in seen]
        seen.update(new)
        order.extend(new)
        stall = 0 if new else stall + 1
        scroll("down")
    return order


def main() -> int:
    try:
        require_front()
        order = scan()
    except Stop as err:
        print(f"STOPPED: {err}")
        return 2
    store.save(store.LABELS_FILE, order)
    print(f"{len(order)} library entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
