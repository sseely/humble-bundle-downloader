"""JSON state files shared by the collection scripts."""

import json
import os
from pathlib import Path
from typing import Any

ENV_STATE_DIR = "HUMBLE_DL_COLLECTIONS_DIR"
STATE_DIR = Path(os.environ.get(ENV_STATE_DIR) or Path.home() / ".humble-dl" / "collections")

LABELS_FILE = "library_items.json"  # every Books library label, from scan.py
PLAN_FILE = "plan.json"  # label -> collections, from plan.py
DUPLICATES_FILE = "duplicates.json"  # file-named copies left out of the plan
PROGRESS_FILE = "progress.json"  # label (no reading status) -> collections added
LEFTOVER_FILE = "leftover.json"  # adds still pending after a run
BOOK_INFO_FILE = "book_info.json"  # label -> new title/author, from info.py (review it)
INFO_SKIPPED_FILE = "book_info_skipped.json"  # file-named labels info.py could not title
LOOKUP_CACHE_FILE = "lookup_cache.json"  # Comic Vine / Open Library responses (no key)
OVERRIDES_FILE = "title_overrides.json"  # hand-written titles/authors/duplicates (edit it)
REMOVE_FILE = "remove.json"  # duplicate label -> copy kept, from info.py (review it)
REMOVED_FILE = "removed.json"  # labels (no reading status) removed by remove_pass.py
RENAMED_FILE = "renamed.json"  # old label (no reading status) -> new one, from rename_pass.py


def load(name: str, default: Any = None) -> Any:
    path = STATE_DIR / name
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save(name: str, data: Any) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / f"{name}.tmp"
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_DIR / name)
