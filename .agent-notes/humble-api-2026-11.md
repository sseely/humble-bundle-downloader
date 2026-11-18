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
