# Architecture overview — humble-bundle-downloader

> **Status: PROPOSED (2026-11-18).** No code exists yet. This describes the
> design locked in the mission brief `plans/humble-ebook-downloader/`
> (gitignored). After that mission merges, re-verify every section against
> `src/` and drop this banner. Future `/plan-mission` runs: treat any
> mismatch between this doc and `src/` as this doc being stale.

## What the system does

`humble-dl` is a single-user Python CLI that mirrors the **ebooks** in a
Humble Bundle library to local disk — every format (PDF, EPUB, MOBI,
PDF (HQ), CBZ, …) and every version. It exists because existing downloaders
broke after Humble's November 2026 site redesign.

- `humble-dl login` — opens a real Chromium window on a persistent profile;
  the human completes login (password or emailed code).
- `humble-dl sync [--dry-run] [--only TEXT] [--output PATH]` — fetches the
  library through the logged-in browser context, plans, and downloads.
- `humble-dl capture` — records and sanitizes the site's library traffic
  into test fixtures (used when the site's API changes).

## Repos

Single repo; no related repos (no GitHub remote as of 2026-11-18).

## Tech stack

| Repo | Language | Runtime | Framework | Database | External deps |
|------|----------|---------|-----------|----------|---------------|
| humble-bundle-downloader | Python | CPython 3.13 (uv) | typer CLI, Playwright, httpx, pydantic | none (filesystem is truth) | www.humblebundle.com (undocumented API), dl.humble.com (signed CDN URLs) |

## Key data flows

1. **Login** — headed Playwright persistent context → human logs in →
   session cookies persist in `~/.humble-dl/profile` (0700).
2. **Sync** — headless context → in-browser `GET /api/v1/user/order` →
   `GET /api/v1/order/{gamekey}?all_tpkds=true` per order (≤2 req/s) →
   pure planner diffs library vs disk → httpx streams each file to `.part`,
   verifies size + md5, `os.replace` into place.
3. **Expired URL** — 403 from CDN → re-fetch that order once → retry.

Diagrams: [architecture.md](architecture.md), [components.md](components.md),
[data-flow.md](data-flow.md).

## Durable contracts

- **On-disk layout** (the only contract users depend on):
  `~/HumbleBundle/<Bundle Name>/<Item Name>/<filename>`; both folder names
  human-readable and sanitized; superseded versions renamed
  `<stem>.<md5[:8]><suffix>`. Changing naming rules re-downloads
  everything — treat as a breaking change needing an ADR.
- **Exit codes:** 0 ok, 1 config / disk space, 2 session expired,
  3 schema drift, 4 file failures.
- **Never deletes or overwrites a completed file.**

## For future planning

- **Scope today: ebooks only** (`platform == "ebook"`). Games, audio,
  video, and keys were explicitly excluded by the user; adding them is new
  scope, and they would reuse planner/downloader but need their own
  folder rules.
- **Biggest external risk:** the Humble API is undocumented and was changed
  without notice in Nov 2026. See [external-api.md](external-api.md) for
  what is known, what is unverified, and how `capture` regenerates
  fixtures.
- **Functional core / imperative shell:** `lib/naming`, `services/planner`,
  `models/library`, `api/capture.sanitize` are pure; put new decisions
  there, keep I/O modules thin.
- **Gate commands:** `uv run pytest --cov --cov-fail-under=90`,
  `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`.
- **Secrets on disk:** browser profile (cookies), gamekeys (grant order
  access), signed URL query strings. Never log or commit any of them.

## Index

| Doc | Contents |
|-----|----------|
| [inventory.md](inventory.md) | Repo inventory table |
| [architecture.md](architecture.md) | System context diagram |
| [components.md](components.md) | Internal module diagram + responsibilities |
| [data-flow.md](data-flow.md) | Sync, URL refresh, file state machine |
| [decisions.md](decisions.md) | ADRs D1–D8 (durable copy of the brief's decisions) |
| [external-api.md](external-api.md) | Humble API knowledge, evidence, unknowns |
| [tech-health.md](tech-health.md) | Runtime/dependency EOL + CVE status |
