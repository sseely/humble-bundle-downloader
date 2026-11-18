# Architecture decisions (ADRs)

Confirmed by the user on 2026-11-18 during `/plan-mission`. Durable copy of
`plans/humble-ebook-downloader/decisions.md` (plans/ is gitignored). Revisit
an ADR by adding a superseding entry, not by editing history.

| ID | Decision |
|----|----------|
| D1 | Python 3.13 + uv + pytest + ruff + pyright |
| D2 | Human completes login in headed persistent Playwright profile |
| D3 | Library JSON fetched inside browser context |
| D4 | Files streamed with httpx from signed URLs |
| D5 | Completion = final file + size + md5; no manifest |
| D6 | All ebook formats; changed md5 → archive old, fetch new |
| D7 | Human-readable sanitized folder names (bundle and item) |
| D8 | Human-run `capture` produces sanitized fixtures mid-mission |

## Evidence base

- Nov 2026 redesign removed 2FA; login by password or emailed code; library
  reportedly incomplete (privacyguides.org, pivot-to-ai.com, 2026-11-13).
- Legacy API: `GET /api/v1/user/order` → `[{gamekey}]`;
  `GET /api/v1/order/{gamekey}?all_tpkds=true` → order with
  `product.human_name`, `subproducts[].{machine_name,human_name,downloads[]}`,
  `downloads[].{platform,download_struct[]}`,
  `download_struct[].{name,url.web,md5,sha1,file_size}` (humble-cli source).
- Humble denies `/api/v1/user/order` to non-browser clients while a
  logged-in browser succeeds (lutris #4099, #5612, #5958).
- **Post-redesign API shape is unverified** — hence D8.

## D1 — Language and tooling
- Context: greenfield CLI; Playwright is first-class in Python; httpx streams
  async.
- Decision: Python 3.13, uv, pytest, ruff, pyright strict.
- Rejected: TypeScript/Node 25 (hand-rolled stream plumbing, no gain).
- Consequences: Playwright browsers installed via
  `uv run playwright install chromium`.

## D2 — Login
- Context: emailed-code login needs a human regardless; bot detection risk.
- Decision: `humble-dl login` opens headed Chromium on persistent profile
  `~/.humble-dl/profile` (0700); waits until session check passes; later runs
  reuse the profile headless.
- Rejected: Keychain password autofill (stores secret, brittle to form
  changes); cookie paste (manual, short-lived).
- Consequences: no credentials stored; session expiry → exit 2.

## D3 — Library data source
- Context: non-browser clients blocked historically.
- Decision: issue API calls via Playwright `context.request` (or
  `page.evaluate(fetch)` if `context.request` is blocked) so cookies and
  fingerprint match the real browser.
- Rejected: plain httpx with copied cookies; DOM scraping (only if capture
  proves API gone — that is a stop condition, not a push-forward).

## D4 — File transfer
- Context: ebook files can be hundreds of MB; `context.request` buffers whole
  bodies in memory; Playwright downloads are serial and awkward.
- Decision: httpx `AsyncClient.stream` of signed `url.web` into `.part`,
  bounded concurrency (default 4).
- Rejected: Playwright download events; `context.request`.
- Consequences: if CDN rejects httpx → stop condition.

## D5 — Completion tracking
- Decision: done ⇔ final path exists ∧ size matches ∧ md5 matches (when the
  API provides them). `.part` files restart from zero. An md5 cache keyed by
  `(path, size, mtime_ns)` at `~/.humble-dl/hash-cache.json` is an
  optimization only; disk is truth.
- Rejected: SQLite or JSON manifest (can drift from disk).

## D6 — Every version
- Decision: every ebook `download_struct` entry (PDF, EPUB, MOBI, PDF (HQ),
  CBZ, …) is fetched. If the target filename exists with a different md5,
  rename it to `<stem>.<md5[:8]><suffix>` (md5 of the *local* file) and
  fetch the new one. Never delete, never overwrite.

## D7 — Naming (user override of PascalCase)
- Decision: bundle folder = sanitized `product.human_name`; item folder =
  sanitized `subproduct.human_name`; filename = basename of the URL path.
- Sanitize: NFC normalize; replace `/` and `:` with ` - ` (collapse repeated
  whitespace); strip control chars; trim trailing dots/spaces; cap at 255
  UTF-8 bytes without splitting a code point.
- Same book in two bundles → copied under both. Two items in one bundle with
  the same sanitized name → second and later get `-<machine_name>`.
- Output root fixed at `~/HumbleBundle`; `--output` override exists for tests
  and verification.

## D8 — Recon
- Decision: `humble-dl capture` (human-run at the Batch 5 checkpoint) records
  library traffic, sanitizes it (consistent fake gamekeys; strip emails,
  cookies, URL query tokens), writes fixtures to `tests/helpers/fixtures/`.
- Consequences: Batch 6 models are reconciled against real fixtures.

## Irreversible changes

None. The tool only creates and renames files.
