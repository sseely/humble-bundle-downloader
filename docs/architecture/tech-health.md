# Tech health

Checked 2026-11-18 against planned pins (no lockfile yet).

| Repo | Component | Version | EOL Date | CVEs (High+) | Action |
|------|-----------|---------|----------|--------------|--------|
| humble-bundle-downloader | CPython | 3.13 | 2029-10 (security-only phase now) | none known | OK — consider 3.14 (bugfix phase, EOL 2030-10) |
| humble-bundle-downloader | playwright | 1.65.2 | — | 0 (OSV) | OK |
| humble-bundle-downloader | httpx | 0.28.1 | — | 0 (OSV) | OK |
| humble-bundle-downloader | typer | 0.29.2 | — | 0 (OSV) | OK |
| humble-bundle-downloader | pydantic | 2.14.3 | — | 0 (OSV) | OK |
| humble-bundle-downloader | pytest | 9.2.0 | — | 0 (OSV) | OK |
| humble-bundle-downloader | pytest-httpx | 0.36.2 | — | 0 (OSV) | OK |

## Sources

- Python status: https://devguide.python.org/versions/ (3.13 = security,
  3.14 = bugfix, retrieved 2026-11-18).
- CVEs: `POST https://api.osv.dev/v1/query` per package+version, PyPI
  ecosystem, 2026-11-18 — zero advisories each.
- Versions: PyPI JSON API, 2026-11-18.

Re-run after the first `uv.lock` exists, against locked versions.
