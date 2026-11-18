# Humble Bundle external API

Undocumented. Humble publishes no developer API. Everything here is
community-reverse-engineered or observed; confidence is marked per claim.

## Status as of 2026-11-18

| Claim | Confidence | Evidence |
|-------|------------|----------|
| Nov 2026 redesign removed 2FA; login is password or emailed code | HIGH | privacyguides.org 2026-11-13; pivot-to-ai.com 2026-11-13 |
| Redesigned library page omits items / is unreliable | MEDIUM | user reports cited by the above |
| Legacy order endpoints still serve after the redesign | **UNKNOWN** | not verified — the first `humble-dl capture` answers this |
| `/api/v1/user/order` is denied to non-browser clients but works from a logged-in browser | MEDIUM | lutris #4099 (2022), #5612, #5958 (2025) |

## Legacy endpoints (pre-redesign)

| Endpoint | Returns |
|----------|---------|
| `GET /api/v1/user/order` | `[{"gamekey": "..."}]` |
| `GET /api/v1/order/{gamekey}?all_tpkds=true` | order object (below) |

Order shape (fields we use; source: humble-cli `humble_api.rs`, docs.rs):

```
product.human_name                       -> bundle folder name
subproducts[].machine_name               -> stable item id
subproducts[].human_name                 -> item folder name
subproducts[].downloads[].platform       -> "ebook" | "windows" | "audio" | ...
subproducts[].downloads[].download_struct[]:
    name          -> format label ("PDF", "EPUB", "MOBI", "PDF (HQ)", ...)
    url.web       -> signed https://dl.humble.com/...?gamekey=...&ttl=...&t=...
    md5, sha1     -> checksums (sometimes absent)
    file_size     -> bytes (sometimes absent)
```

## Sensitive values

- **gamekey** — grants access to an order without login. Redact in logs
  (`abcd…`), remap to `GAMEKEY_NNNN` in fixtures.
- **Signed URL query** (`gamekey`, `ttl`, `t`) — strip before logging.
- **Session cookies** — live only in `~/.humble-dl/profile` (0700).

## When the API changes again

1. `sync` exits 3 and writes the offending raw response to
   `~/.humble-dl/debug/` (outside the repo, mode 0600).
2. Run `humble-dl capture` → sanitized fixtures in
   `tests/helpers/fixtures/captured/` + `INDEX.md`.
3. Reconcile `models/library.py` against the new fixtures (aliases /
   optional fields only, keeping the domain interface stable).
4. If the JSON API is gone entirely, the fallback is scraping the library
   DOM — a new ADR, not a patch.

## Sources

- https://www.privacyguides.org/news/2026/11/13/humble-bundle-redesign-removes-2fa-and-other-important-account-security-features/
- https://pivot-to-ai.com/2026/11/13/humble-bundle-vibe-code-update-breaks-all-user-security/
- https://docs.rs/crate/humble-cli/0.20.0/source/src/humble_api.rs
- https://github.com/lutris/lutris/issues/4099
- https://github.com/lutris/lutris/issues/5612
- https://github.com/lutris/lutris/issues/5958
