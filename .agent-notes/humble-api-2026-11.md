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

## Observation: Playwright headless-shell cannot read a headed profile's cookies
- **Context**: `humble-dl login` (headed) persisted the Ory session; `capture` (headless) got whoami 401.
- **Finding**: Playwright 1.65 runs `headless=True` without a channel on the separate `chromium-headless-shell` binary (driver coreBundle.js `getExecutableName`). It loaded none of the encrypted (v11) cookies headed Chromium wrote, and a later headed run also saw none, consistent with the shell discarding them. `channel="chromium"` (same binary, new headless) loads them and whoami returns 200.
- **Impact**: Any persistent-profile context shared between headed and headless runs must pass `channel="chromium"`.
- **Confidence**: High (controlled back-to-back experiment on the same profile)

## Observation: library list omits per-file data; files live on the bundle detail page (2026-11-19)
- **Context**: First successful `humble-dl capture` of `/home/library` after the redesign.
- **Finding**: The list page's RSC flight data carries `bundleItems[]` (id = productEntitlementId UUID, title, `type: "bundle"`, `itemType: "content-entitlement"`, `entitlements.downloads[]`) with per-file `downloads` and `signedUrls` set to `"$undefined"`. Each bundle's books and format download buttons (e.g. "EPUB (2.9 MB)", format selector, Bulk Download) are on `/home/library/<productEntitlementId>`. Rows are not `<a href>` links. The page also has "Purchased" and "Bundles" tabs. Two Next.js server-action POSTs to `/home/library` returned 403 under capture.
- **Impact**: The library client must enumerate bundle ids from the list page, then read each detail page. A "still migrating" banner on the page does not mean files are missing.
- **Confidence**: High (capture + human screenshots)

## Observation: Cloudflare blocks server-action POSTs from headless Chromium (2026-11-19)
- **Context**: Capturing the signed-URL request on `/home/library/<bundle id>`.
- **Finding**: Next.js server-action POSTs (`Next-Action` header) from headless Chromium get 403 with Cloudflare's "Attention Required!" page; the same requests from headed Chromium get 200. Page GETs succeed headless. The only differing request header seen was the UA (`HeadlessChrome/...` vs `Chrome/...`). `getSignedUrlsForEntitlements([entitlementId])` returns `data.signedUrls[subproductMachineName][downloadKey][filename] = "https://cdn.humble.com/humblebundle/<filename>?<signature>"`. Action ids are per-deploy (`70f766da...` getSignedUrls, `40528d76...` recordDownload at time of writing).
- **Impact**: Signed URLs require a headed browser (or a non-headless fingerprint). Files are served from `cdn.humble.com`, not `dl.humble.com`.
- **Confidence**: High (back-to-back headless/headed runs, same profile)
