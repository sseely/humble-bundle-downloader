# humble-dl

`humble-dl` downloads every ebook file in your Humble Bundle library: every
format (PDF, EPUB, MOBI, …) and every version Humble has published. Files go
to `~/HumbleBundle/<Bundle>/<Item>/<file>`.

Only ebooks are downloaded. Games, audio, video and keys are ignored.

## Requirements

- macOS or Linux, Python 3.13, [uv](https://docs.astral.sh/uv/)
- A Humble Bundle account

## Install

```bash
uv sync
uv run playwright install chromium
```

## Usage

```bash
uv run humble-dl login                          # sign in once; the session is saved
uv run humble-dl sync --dry-run                 # list what would be downloaded
uv run humble-dl sync                           # download everything new or changed
uv run humble-dl sync --only "scalzi"           # only bundles whose title contains the text
uv run humble-dl sync --output /Volumes/Books   # download somewhere other than ~/HumbleBundle
uv run humble-dl capture                        # record sanitized site data for debugging
```

### A browser window opens

`login`, `sync` and `capture` drive a real Chromium window. Leave it alone
while the command runs; it closes by itself.

The window has to be visible. Humble's Cloudflare protection blocks
download-link requests from a hidden (headless) browser.

### Logging in

`login` opens Humble's sign-in page. Sign in (including any two-factor step)
and wait for the command to finish. The session is saved in
`~/.humble-dl/profile` and lasts about 90 days.

> **The profile directory is a secret.** Anyone with a copy of
> `~/.humble-dl/profile` can use your Humble account. Never commit, share or
> back it up to a shared location. Set `HUMBLE_DL_PROFILE_DIR` to keep it
> elsewhere.

## Apple Books (macOS)

```bash
uv run humble-dl books --dry-run       # list what would be sent to Books
uv run humble-dl books                 # import new books into Apple Books
uv run humble-dl books --only "scalzi" # only bundles whose folder name matches
uv run humble-dl books-duplicates      # list books that are in Books more than once
uv run humble-dl sync && uv run humble-dl books   # download, then import new books
```

`books` sends files to Apple Books with `open -a Books`, one file per book:

- **Format:** the EPUB when an item has one, otherwise the PDF. MOBI files,
  unfinished downloads and archived old versions are never sent.
- **Overlapping bundles:** when the same book appears in several bundles, it
  is sent once. Two files count as the same book when they share a file name,
  an EPUB title or a Humble item title. Editions and store variants (Kindle,
  Kobo, HQ, high/low) count as the same book.
- **Already in Books:** books are skipped when they are already in Apple
  Books' iCloud library
  (`~/Library/Mobile Documents/iCloud~com~apple~iBooks/Documents`). That folder
  names EPUBs by title and PDFs by file name. `books` only reads entry names
  there and never changes anything.
- **Never twice:** every import is recorded in
  `~/.humble-dl/books-imported.json`, so a file is sent once. A changed file
  (a new version) is sent again.

Files go to Books one at a time, with a pause between them (`--pause`,
default 1 s). A file counts as imported only once its new entry shows up in
Books' library folder; `books` waits up to 60 s for it, then records it. A file
that never appears is listed, makes the command exit 4, and is retried on the
next run. (Sending several files per launch made Books silently drop some.)

Books copies every imported file into its own library. With iCloud enabled for
Books, imports also upload to iCloud and count against your storage.

`books-duplicates` only reports. To remove an extra copy, use the Books app:
right-click the book, then choose **Remove**, then **Remove Everywhere**.

### Everything in one go

```bash
./sync-books.sh          # asks before retitling and before deleting copies
./sync-books.sh --yes    # unattended
```

It runs every step below in order: download (`humble-dl sync`), Humble titles
(`humble-dl titles`), import into Books (`humble-dl books`), plan titles,
authors and duplicates (`books-collections.sh --info`), retitle (`--rename`),
delete duplicate copies (`--remove`), then file books into collections. Books
are retitled and duplicates deleted before collections are planned, so each
book is filed under its final title and no copy about to be deleted is filed.
Before the retitle and delete steps it shows how many entries each will change
and the file to review (`book_info.json`, `remove.json`). A step that finishes
with some files failed or some books left over (for example books stored only
in iCloud) is reported at the end and the run continues; anything else stops
it. The Books steps need Books in front and the Mac left alone.

### One collection per bundle

`books` imports books but does not sort them into collections. Run
`books-collections.sh` from the repository root after importing:

```bash
uv run humble-dl books && ./books-collections.sh   # import, then file into collections
./books-collections.sh --plan                       # scan and plan only; changes nothing
```

Each book goes into a Books collection named after its bundle folder (for
example "Humble Comics Bundle - Example Comics
by Oni Press"). A book that appears in several bundles goes into each of their
collections. Books that match no bundle go into **My Books**. Missing
collections are created.

Apple Books has no scripting support for collections, so the script uses the
Books app itself through macOS accessibility. It right-clicks each book and
chooses **Add to Collection**. It never writes Books' database.

- **Permissions:** the terminal app needs **Accessibility** permission
  (System Settings → Privacy & Security → Accessibility).
- **Keep Books in front:** Books ignores the script unless it is the frontmost
  app. The script stops if another app takes focus or the screen locks, and
  keeps the Mac awake while it runs (`caffeinate`). Don't use the Mac until it
  finishes.
- **Key presses:** to name a new collection, the script pastes the name with
  ⌘V and presses Return, both sent to Books only. Your clipboard is restored
  afterwards.
- **Re-run at any time:** every add is recorded in
  `~/.humble-dl/collections/progress.json`. A re-run skips books that are
  already in their collections, even when their reading progress has
  changed. Set `HUMBLE_DL_COLLECTIONS_DIR` to keep that state somewhere else.
- **Duplicates:** when Books holds a book under both its title and its file
  name (`rickandmorty_vol1`), only the titled copy is filed. The file-named
  copies are listed in `duplicates.json` in the same folder.
- **Safety checks:** before each add, the script compares every collection's
  book count with what it expects. On a mismatch (for example, you changed a
  collection while it ran) it stops without changing anything else.

It runs three scripts from `scripts/books_collections/`: `scan.py` records
every entry in the Books library list, `plan.py` matches entries to bundle
folders, and `run_pass.py` makes the adds. They need the `macos` dependency
group, which `books-collections.sh` loads with `uv run --group macos`.

### Readable titles and authors

Books titles a PDF that carries no metadata after its file name
(`rickandmortypresents_vol5`) and shows "Unknown Author". Two steps fix that:

```bash
uv run humble-dl titles                # save Humble's exact item titles
./books-collections.sh --info          # scan Books, look up titles and authors; changes nothing
./books-collections.sh --rename        # apply them in Books
```

- **Titles:** `humble-dl titles` saves each item's title as Humble lists it
  (`Rick and Morty: Worlds Apart`, colons and all) to
  `~/.humble-dl/titles.json`. File-named entries get that title. Entries that
  match no Humble item, and file-named copies of a book Books already holds
  under its title, are left alone and listed in `book_info_skipped.json` and
  `duplicates.json`.
- **Authors:** every entry showing "Unknown Author" gets writer credits from
  [Comic Vine](https://comicvine.gamespot.com/api/), matched by series name,
  issue or volume number, the publisher in the bundle name ("by Oni Press")
  and the year in the PDF's metadata. Without a Comic Vine match, the first
  contributor [Open Library](https://openlibrary.org) lists is used; that
  can be an artist, so check those. Entries with no match keep "Unknown
  Author".
- **Review first:** `--info` only writes `book_info.json` (in
  `~/.humble-dl/collections/`): each entry's new title, author, and where the
  author came from. Edit or delete entries there before running `--rename`.
- **Comic Vine key:** get a free key at https://comicvine.gamespot.com/api/
  (log in first) and store it in the Keychain, where `--info` reads it:
  `security add-generic-password -a "$USER" -s humble-dl-comicvine -w`.
  Without a key, only Open Library is used. Responses are cached in
  `lookup_cache.json`; the key is never written to disk.
- **What `--rename` does:** right-click → **Rename** opens Books' Book Info
  sheet; the script fills in Title and Author and presses OK. Books also
  renames the book's file in its iCloud folder. Each rename is recorded in
  `renamed.json` and the book's collection progress follows it, so re-runs
  skip finished entries and `books-collections.sh` won't add the book again.
  The same rules as collections apply: Books in front, hands off the Mac.
- **Your own titles:** for entries Humble has no title for (for example old
  2014 copies such as `RedSonjaVol1-03HQ_1418163538`), write them in
  `~/.humble-dl/collections/title_overrides.json`, keyed by the Books label
  without its reading status: `{"RedSonjaVol1-03HQ_1418163538, Unknown
  Author": {"title": "Red Sonja (2005) #3", "author": "Michael Avon Oeming &
  Mike Carey"}}`. Overrides win over lookups. Mark an extra copy with
  `{"duplicate_of": "<the copy you keep>"}` to have it removed instead.
- **Books stored only in iCloud:** Books offers no **Rename** for a book that
  isn't downloaded (its label ends in "In iCloud"). `--rename` skips those and
  lists them at the end; download them in Books, then run `--rename` again.

### Remove duplicate copies

`--info` also writes `remove.json`: every file-named copy of a book Books
already holds under its title (`purgatori_issue3` next to `Purgatori #3`), plus
overrides marked `duplicate_of`, each with the copy that is kept. Review it,
then:

```bash
./books-collections.sh --remove
```

For each listed copy, the script right-clicks → **Remove…**, presses
**Delete Everywhere** and answers Books' "Delete from iCloud?" with **Delete**,
which deletes that copy from iCloud on all your devices.
The kept copy is not touched. It checks that the entry left the library before
moving on and stops on anything unexpected. Removals are recorded in
`removed.json`, so re-runs skip them.

## Folder layout

For the bundle "Humble Book Bundle: The John Scalzi Collection: Old Man's
War & More by TOR Books", files land at paths like these:

```text
~/HumbleBundle/Humble Book Bundle - The John Scalzi Collection - Old Man's War & More by TOR Books/The Kaiju Preservation Society/kaijupreservationsociety_johnscalzi.epub
~/HumbleBundle/Humble Book Bundle - The John Scalzi Collection - Old Man's War & More by TOR Books/Redshirts/redshirts_johnscalzi.epub
~/HumbleBundle/Humble Book Bundle - The John Scalzi Collection - Old Man's War & More by TOR Books/Redshirts/redshirts_johnscalzi.1a2b3c4d.epub
```

The last path is an older version of the same book, kept alongside the
current one (see below).

- Bundle and item folder names come from Humble's titles; `:` becomes ` -`. Characters that
  are not allowed in file names are replaced, and names are cut to 255 bytes.
- Folder names are compared case-insensitively, because default macOS volumes
  are case-insensitive. When two items in a bundle would get the same folder,
  the later one gets a `-<machine name>` suffix.
- File names are Humble's own.

## Versions are kept, never overwritten

`humble-dl` never deletes or overwrites a downloaded file.

When Humble publishes a new version of a file (its md5 changes), the existing
file is renamed to `<stem>.<first 8 characters of its md5><suffix>`, for
example `book.pdf` → `book.1a2b3c4d.pdf`. The new version is then downloaded
as `book.pdf`. Over time you keep every version.

## Resuming

Run `sync` again at any time; it picks up where it stopped.

- A file counts as done when it exists with the expected size and md5.
- Partial downloads are written to `<file>.part` and restart from zero on the
  next run. A file only appears under its final name once it is complete and
  verified.
- Completed files are skipped. A second run of an up-to-date library downloads
  nothing.
- md5 results are cached in `~/.humble-dl/hash-cache.json` so unchanged files
  are not re-hashed. The cache is only an optimisation; the files on disk are
  the source of truth.

Before downloading, `sync` checks that the output disk has room for the
planned files and stops without writing anything if it does not.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Configuration, disk space, library or browser error |
| 2 | Not logged in, session expired, or a bot challenge |
| 3 | Humble's data no longer matches the expected format |
| 4 | One or more files failed to download, or a bundle could not be loaded (each is listed) |

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `HUMBLE_DL_CONCURRENCY` | `4` | Parallel file downloads (1–32) |
| `HUMBLE_DL_PROFILE_DIR` | `~/.humble-dl/profile` | Browser profile holding the session |

## Troubleshooting

**Exit 2: "Session expired or not logged in."**
Run `uv run humble-dl login` and sign in again, then re-run `sync`. If the
message mentions a bot challenge, wait a while before retrying.

**Exit 3: "Humble Bundle's data no longer matches the expected format."**
Humble changed its site. The raw response is saved in `~/.humble-dl/debug/`.
Run `uv run humble-dl capture` to record sanitized site data, then update the
parsers in `src/humble_dl/models/library.py` (or open an issue with the
capture). Debug files can contain short-lived download links; do not share
them.

**Exit 4: some files or bundles failed.**
Usually a network problem, an expired download link, or a temporary Humble
server error (`upstream_error`). Re-run `sync`; files that already finished
are skipped and the failed ones are retried. Other bundles still download
when one bundle fails.

## Development

```bash
uv run pytest --cov --cov-fail-under=90
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest -m live        # tests against the real site (needs a login)
```

Sanitized site captures for tests live in `tests/helpers/fixtures/`.
Notes on how the redesigned site works are in `.agent-notes/`.
