# System architecture (proposed)

Single-process CLI on the user's Mac. Two external HTTP dependencies; all
state is local files.

```plantuml
@startuml
!include <C4/C4_Container>

Person(user, "Library owner", "Runs humble-dl; completes login")
System_Boundary(local, "User's machine") {
  Container(cli, "humble-dl", "Python 3.13, typer", "login / sync / capture")
  Container(browser, "Chromium (Playwright)", "persistent context", "Holds Humble session; issues library API calls")
  ContainerDb(out, "~/HumbleBundle", "filesystem", "Ebook files: Bundle/Item/file")
  ContainerDb(app, "~/.humble-dl", "filesystem", "profile/ (0700), hash-cache.json, debug/")
}
System_Ext(hb, "www.humblebundle.com", "Undocumented JSON API + login")
System_Ext(cdn, "dl.humble.com", "Signed, expiring file URLs")

Rel(user, cli, "runs", "terminal")
Rel(user, browser, "logs in (login only)", "headed window")
Rel(cli, browser, "drives", "Playwright")
Rel(browser, hb, "GET /api/v1/user/order, /api/v1/order/{gamekey}", "HTTPS, session cookies")
Rel(cli, cdn, "streams files", "HTTPS, httpx")
Rel(cli, out, "writes .part, os.replace", "file I/O")
Rel(cli, app, "reads/writes cache, debug", "file I/O")
Rel(browser, app, "persists cookies", "profile dir")
@enduml
```

## Communication

| From → To | Protocol | Sync/async | Failure handling |
|-----------|----------|------------|------------------|
| browser → humblebundle.com | HTTPS JSON | sync, ≤2 req/s | 401 → exit 2; 429/5xx backoff ×5; bad JSON → debug dump, exit 3 |
| cli → dl.humble.com | HTTPS stream | async, concurrency 4 | 403 → refresh order once; 5xx/network ×3; md5 mismatch → discard `.part` |
