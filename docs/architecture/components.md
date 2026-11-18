# Components (proposed)

```plantuml
@startuml
skinparam componentStyle rectangle

package "humble_dl" {
  [cli] as CLI
  package "services" {
    [sync] as SYNC
    [planner\n«pure»] as PLAN
  }
  package "workers" {
    [downloader] as DL
  }
  package "api" {
    [client] as CLIENT
    [capture] as CAP
  }
  package "auth" {
    [session] as SESS
  }
  package "models" {
    [library\n«pure»] as MODELS
  }
  package "lib" {
    [naming\n«pure»] as NAME
    [hashcache] as HASH
    [logging] as LOG
  }
  [config] as CFG
}

CLI --> SYNC : run_sync
CLI --> CAP : run_capture
CLI --> SESS : open_context / interactive_login
SYNC --> CLIENT : list_gamekeys / get_order
SYNC --> PLAN : plan()
SYNC --> DL : execute()
SYNC --> HASH : local_file / record / save
PLAN --> NAME : sanitize / filename / versioned_name
PLAN --> MODELS : ebook_items
CLIENT --> SESS : ctx.request
CLIENT --> MODELS : parse_order
CAP --> SESS : ctx, page
DL ..> LOG : file events
CLIENT ..> LOG : api events
SYNC ..> CFG : Settings
@enduml
```

## Responsibilities

| Module | Kind | Responsibility |
|--------|------|----------------|
| `cli` | shell | typer commands; maps exceptions → exit codes |
| `config` | pure loader | `Settings` from injected env + home |
| `auth/session` | shell | persistent Chromium context, session probe, interactive login |
| `api/client` | shell | rate-limited in-browser API calls; drift dumps to debug dir |
| `api/capture` | shell + pure `sanitize` | record traffic → sanitized fixtures |
| `models/library` | pure | pydantic models; `ebook_items` projection |
| `lib/naming` | pure | filesystem-safe names; collision + version naming |
| `lib/hashcache` | shell | `(path,size,mtime_ns) → md5` cache |
| `lib/logging` | infra | JSON logs; URL/gamekey redaction |
| `services/planner` | pure | library + disk view → `Skip` / `Fetch` / `ArchiveThenFetch` |
| `services/sync` | shell | sequences fetch → plan → space check → execute → summary |
| `workers/downloader` | shell | stream `.part`, verify, atomic place, refresh on 403 |
