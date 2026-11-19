## Observation: Humble redesign (Nov 2026) changed login; API unverified
- **Context**: Researching why existing Humble downloaders broke, during
  /plan-mission on 2026-11-18.
- **Finding**: Redesign ~2026-11-11..13 removed 2FA; login is password or
  emailed code. Whether legacy `/api/v1/order/{gamekey}` still works is
  unverified — no capture has been run yet.
- **Impact**: Do not assume the legacy order schema; run `humble-dl capture`
  and build against `tests/helpers/fixtures/captured/`.
- **Confidence**: High (login change), Low (API shape)

## Observation: Humble blocks non-browser clients on the order API
- **Context**: Same research.
- **Finding**: `/api/v1/user/order` returns access-denied to Lutris/GameHub
  style clients while a logged-in browser succeeds (lutris #4099, #5612,
  #5958).
- **Impact**: Issue library API calls from the Playwright context
  (`context.request`), not httpx with copied cookies. CDN file downloads via
  httpx are assumed OK but unverified.
- **Confidence**: Medium

## Observation: Python 3.13 is security-only
- **Context**: Tech-health check, 2026-11-18.
- **Finding**: devguide.python.org lists 3.13 as "security" (EOL 2029-10)
  and 3.14 as "bugfix" (EOL 2030-10). The plan pins 3.13 (D1).
- **Impact**: Fine to ship; 3.14 is the better default if D1 is revisited.
- **Confidence**: High

## Observation: Legacy /api/v1 routes are gone; site is Next.js + Ory Kratos
- **Context**: First `humble-dl login` run (2026-11-19) exited 1: probe
  `/api/v1/user/order` returned 404 on the first poll.
- **Finding**: Logged-out curl: `/api/v1/user/order`, `/api/v1/order/<key>`,
  `/api/v1/user`, `/api/v2/user/order` and `/login` all return the Next.js
  HTML 404 page (same as a nonsense path), not an API 401/JSON error.
  `/home/library` server-redirects (RSC `NEXT_REDIRECT`, 307) to
  `https://auth.humblebundle.com/self-service/login/browser?return_to=...`
  (Ory Kratos). Kratos `GET auth.humblebundle.com/sessions/whoami` → 401 JSON
  when logged out; `GET www.humblebundle.com/api/auth/session-status` → 200
  `{"status":"anonymous"}`. Client JS bundles reference no order/library API
  path; library data is likely server-rendered (RSC), not a JSON API.
- **Impact**: D3 (library JSON via legacy API) is contradicted. Session probe
  and login path in auth/session.py must change; capture must record RSC /
  non-JSON payloads to discover where library data lives.
- **Confidence**: High (routes gone, auth host); Medium (library is RSC —
  needs a logged-in capture to confirm)

## Observation: Kratos session cookie is not persisted by Chromium profiles
- **Context**: After a successful `humble-dl login` (whoami 200), the next
  `humble-dl capture` in a fresh persistent context got whoami 401.
- **Finding**: The profile Cookies DB held only Cloudflare persistent cookies
  (`__cf_bm`, `__cflb` on auth.humblebundle.com); no session cookie. Humble's
  Kratos session cookie has no Expires, and Chromium does not persist
  session cookies in a persistent profile.
- **Impact**: Login must re-add expiry-less humblebundle.com cookies with the
  Kratos session `expires_at` before closing the context, or every later
  command is logged out.
- **Confidence**: High
