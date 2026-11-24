"""Match scanned Books labels to bundle folders and write plan.json.

Also moves progress records to labels without reading status, so books whose
progress changed since the last run are not added twice.
"""

import os
from pathlib import Path

import store

from humble_dl.config import load_settings
from humble_dl.services import books_collections as bc


def main() -> int:
    labels: list[str] | None = store.load(store.LABELS_FILE)
    if labels is None:
        print(f"No {store.LABELS_FILE} in {store.STATE_DIR}; run scan.py first")
        return 1
    output_dir = load_settings(os.environ, Path.home()).output_dir
    assigned, unmatched = bc.assign_labels(labels, bc.owner_index(output_dir))
    plan = bc.build_plan(labels, assigned)
    done = bc.rekey_progress(store.load(store.PROGRESS_FILE, {}))
    store.save(store.PLAN_FILE, plan.collections)
    store.save(store.DUPLICATES_FILE, plan.duplicates)
    store.save(store.PROGRESS_FILE, done)
    todo = bc.pending(plan.collections, done)
    print(f"books: {len(plan.collections)}  file-named duplicates skipped: {len(plan.duplicates)}")
    for name, count in plan.counts():
        print(f"  {count:4} {name}")
    print(f"not matched to a bundle (go to {bc.UNFILED_COLLECTION}): {len(unmatched)}")
    print(f"adds pending: {sum(map(len, todo.values()))} for {len(todo)} books")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
