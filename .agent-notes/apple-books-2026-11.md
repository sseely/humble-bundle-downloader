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
