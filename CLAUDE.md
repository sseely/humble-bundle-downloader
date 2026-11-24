# humble-dl

Python CLI that downloads every **ebook** file (all formats, all versions)
from a Humble Bundle library to `~/HumbleBundle/<Bundle>/<Item>/<file>`.

## Scope

Ebooks only (`platform == "ebook"`). Games, audio, video, and keys are out of
scope.

## Stack

- Python 3.13, uv-managed, src layout, package `humble_dl`
- Runtime: playwright (login + library API in browser context), httpx
  (streams signed file URLs), typer (CLI), pydantic (models)
- Dev: pytest (+asyncio, cov, httpx), ruff, pyright strict

## Layout

```
src/humble_dl/
  cli.py  config.py
  auth/session.py            headed persistent-profile login
  api/client.py api/capture.py   library JSON via browser context; fixtures
  api/bookmeta.py            Comic Vine / Open Library writer lookups (httpx)
  models/library.py          pydantic models of the order API
  lib/naming.py lib/logging.py lib/hashcache.py lib/pdfmeta.py
  services/planner.py services/sync.py services/titles.py
  services/books.py services/books_library.py services/books_collections.py
  services/book_info.py      Books retitles and writer matching (pure)
  workers/downloader.py      httpx streaming into .part files
tests/   mirrors src; shared helpers and fixtures in tests/helpers/
scripts/books_collections/   Books UI automation (pyobjc, `macos` group);
                             run via ./books-collections.sh
sync-books.sh                full pipeline: sync, titles, import, retitle,
                             remove duplicates, collections
```

## Quality gates (all must pass before commit)

```bash
uv run pytest --cov --cov-fail-under=90
uv run ruff check
uv run ruff format --check
uv run pyright
```

Tests marked `live` hit the real service and are excluded by default; run
them with `uv run pytest -m live`.

## Invariants

- Never delete or overwrite a completed downloaded file; a changed md5
  archives the old file as `<stem>.<md5[:8]><suffix>`.
- The on-disk naming layout is a durable contract (naming snapshot tests).
- Never log or commit cookies, raw gamekeys, emails, or signed URL queries.
- The Comic Vine API key comes from `HUMBLE_DL_COMICVINE_API_KEY` (the
  launcher reads the `humble-dl-comicvine` Keychain entry); never cache,
  log, or commit it.
- Tests never write to `~/HumbleBundle`.
