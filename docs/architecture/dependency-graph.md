# Dependency Graph

_Last Updated: 2026-04-07_

## Description

Module dependencies and package relationships in the codebase, including the livestream service integration with f2.

<!--@auto:diagram:deps:start-->

```mermaid
graph LR
    Main[main.py] --> RouterU[routers/users]
    Main --> RouterP[routers/posts]
    Main --> RouterL[routers/livestreams]
    Main --> Core[core/*]

    RouterU --> SvcU[services/users]
    RouterP --> SvcP[services/posts]
    RouterL --> SvcL[services/livestreams]

    SvcU --> SchU[schemas/users]
    SvcP --> SchP[schemas/posts]
    SvcL --> SchL[schemas/livestreams]

    SvcU --> Douyin[Douyin API via f2]
    SvcP --> Douyin
    SvcL --> Douyin
    SvcL --> SvcU

    SvcL --> F2DL[f2 DouyinDownloader]
    SvcL --> F2Util[f2 WebCastIdFetcher]

    SvcU --> Storage[services/storage]
    Storage --> R2[Cloudflare R2 via boto3]

    Core --> Settings[core/settings]
    Core --> Exceptions[core/exceptions]
    Core --> Logging[core/logging]
    Core --> Deps[core/dependencies]

    Deps --> SvcU
    Settings --> PydanticSettings[pydantic-settings]
```

<!--@auto:diagram:deps:end-->
