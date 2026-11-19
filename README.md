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
