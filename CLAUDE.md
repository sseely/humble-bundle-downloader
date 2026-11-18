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
  models/library.py          pydantic models of the order API
  lib/naming.py lib/logging.py lib/hashcache.py
  services/planner.py services/sync.py
  workers/downloader.py      httpx streaming into .part files
tests/   mirrors src; shared helpers and fixtures in tests/helpers/
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
- Tests never write to `~/HumbleBundle`.
