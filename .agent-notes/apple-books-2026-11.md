# Apple Books library (macOS) — observations

## Observation: where Books keeps imported books, and how it names them
- **Context**: Building `humble-dl books` (2026-11-19).
- **Finding**: Books' private container (`~/Library/Containers/com.apple.iBooksX/...`) is blocked by macOS privacy protection ("Operation not permitted") without Full Disk Access. With iCloud enabled for Books, imported books live in `~/Library/Mobile Documents/iCloud~com~apple~iBooks/Documents`, which is readable. EPUBs are stored as unpacked *directories* named by the book's title with punctuation stripped (`Old Mans War.epub`); extra copies get ` 1`, ` 2` suffixes (`Tress of the Emerald Sea 1.epub`). PDFs keep their original filename, sometimes with `_<10 digits>`. Numbered comic issues (`Chastity Volume 1 2.epub`) look like copy suffixes but are not.
- **Impact**: Match downloads against the library by EPUB `dc:title` and Humble item title, not filename alone. Treat `Title N` as a copy only when `Title` exists. Never write to this folder; remove duplicates in the Books app ("Remove Everywhere").
- **Confidence**: High (inspected live library: 367 entries)

## Observation: `open -a Books` imports and de-duplicates identical files
- **Context**: Importing 6 Kickstarter books with `open -a Books <files>`.
- **Finding**: New entries appeared ~1 min later. Two books already present (same title) produced no new entry. Launching Books also rewrites metadata inside many EPUB directories, so their mtime changes; use `st_birthtime` to find genuinely new entries.
- **Impact**: Recently-modified does not mean recently-imported; check creation time.
- **Confidence**: Medium (one observation)

## Observation: batched `open -a Books` silently drops files
- **Context**: verifying `humble-dl books` (batch 10, pause 5 s) against the Books UI on 2026-11-20
- **Finding**: 62 files were recorded as imported but only 26 appeared in Books; size was not the factor (a 525 MB PDF landed, a 3 MB EPUB did not). Sending each missing file alone with `open -a Books <file>` and waiting for the new library-folder entry landed all 36 in 2-4 s each. A single-file open also opens a reader window per book.
- **Impact**: `open`'s exit status does not prove an import; confirm the new library entry before recording a file as imported.
- **Confidence**: High (observed counts); the cause inside Books is unknown

## Observation: Books UI is scriptable only through accessibility
- **Context**: building per-bundle collections
- **Finding**: Books has no AppleScript dictionary and its App Intents cannot add to collections. The AX tree exposes visible library rows as AXButton "Title, Author, N% finished" with AXShowMenu and AXScrollDown/UpByPage; the context menu has an `addToCollection` item that opens a sheet of collections. Books must be frontmost or AXPress on menu items has no effect.
- **Impact**: collection automation needs Accessibility permission for the terminal and Books in front; reading progress per book is also visible through AX.
- **Confidence**: High

## Observation: Books labels change with reading progress
- **Context**: re-running the collections pass for a new bundle (2026-11-24)
- **Finding**: the AX label of a library row ends in its reading status ("1% finished", "16% finished", "Finished book", optionally ", In iCloud"), so it changes as the user reads. Progress recorded under the full label no longer matched 5 read books, which would have re-added them and tripped the count check. Also, after `humble-dl books` the library window may be closed (only reader windows); `open -a Books` brings it back.
- **Impact**: key any per-book state on `strip_progress(label)` (`services/books_collections.py`), never the raw label. `books-collections.sh` runs `open -a Books` first.
- **Confidence**: High

## Observation: Books "Rename" is a Book Info sheet; renaming renames the iCloud file
- **Context**: probing title fixes for file-named entries (2026-11-24)
- **Finding**: the row context menu item `rename` opens an AXSheet "Book Info" with two AXTextFields (Title, Author; AXValue and AXFocused settable) and Cancel/OK AXButtons. Focus lands on an AXGroup, not the Title field, so paste + Return does nothing. Setting the Title field's AXValue then AXPress on OK works (Books frontmost). The label changes at once, and Books also renames the PDF in `~/Library/Mobile Documents/iCloud~com~apple~iBooks/Documents` to `<new title>.pdf`.
- **Impact**: a rename changes both the AX label (progress.json keys) and the iCloud file name that `books_library` matches imports against. Comic PDFs carry no author metadata (kMDItemAuthors is the authoring app) and CBZs have no ComicInfo.xml; the library API's download entitlements expose `title` but no author.
- **Confidence**: High (one entry renamed: rickandmortypresents_vol5)

## Observation: no Rename for books stored only in iCloud
- **Context**: preparing the rename pass (2026-11-24); reported by the user, then confirmed on one entry
- **Finding**: a book whose AX label ends in ", In iCloud" is not downloaded, and its context menu has no `rename` item; downloading it in Books makes Rename appear (theboys_vol8 showed `rename` and lost the suffix after the user downloaded it). The download item's identifier was not observed.
- **Impact**: `rename_pass.py` skips cloud-only labels (`is_cloud_only`) and menus without Rename, lists them, and a re-run picks them up after the user downloads them.
- **Confidence**: Medium (one entry observed before/after; download menu item not seen)

## Observation: Books scripts assumed the All view; Remove alert buttons
- **Context**: rescanning after the user left a collection selected (2026-11-24)
- **Finding**: with a collection selected, the library window has no AXGroup "Library", so `live_labels()` raised "Books library list not visible". `scan.py`, `run_pass.py` and `rename_pass.py` now call `show_all()` first. Right-click → Remove… on a downloaded book opens an AXSheet alert ("This item was downloaded to this Mac. Delete it from iCloud or remove the download…") with AXButtons titled "Remove Download", "Delete Everywhere", "Cancel" (identifiers `action-button--999/998/997`, not relied on). The alert for a cloud-only book was not observed.
- **Impact**: `remove_pass.py` presses the button titled "Delete Everywhere", cancels and skips if the alert lacks it, and checks the entry left the list.
- **Confidence**: High (observed); cloud-only alert unknown

## Observation: Delete Everywhere asks a second time
- **Context**: first `--remove` run (2026-11-24)
- **Finding**: after "Delete Everywhere", Books shows a second AXSheet: "Delete from iCloud?" / "Are you sure you want to delete this book? It will be removed from iCloud and all your iCloud devices." with "Cancel" and "Delete". The same two alerts appear for a cloud-only book (theboys_vol10, "In iCloud"). All 47 removals then worked; the iCloud folder shrank by exactly 47 entries.
- **Impact**: `remove_pass.py` presses "Delete" only when the sheet text includes "Delete from iCloud?", and stops on any other alert.
- **Confidence**: High
