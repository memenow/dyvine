# Flowchart

_Last Updated: 2026-04-07_

## Description

End-to-end request processing with decisions and data stores, covering all three feature domains: users, posts, and livestreams.

<!--@auto:diagram:flow:start-->

```mermaid
flowchart TD
    A[Client Request] --> B[FastAPI Router]
    B --> C{Valid Route?}
    C -->|No| E[Error Handler]
    C -->|Yes| D{Which Domain?}

    D -->|Users| U[UserService]
    D -->|Posts| P[PostService]
    D -->|Livestreams| L[LivestreamService]

    U --> F[Douyin API]
    P --> F
    L --> F

    F --> G{API Success?}
    G -->|No| E
    G -->|Yes| H[Business Logic]

    H --> S{Storage Required?}
    S -->|Yes| R[Cloudflare R2]
    S -->|No| I[Response Formatter]
    R --> I

    L --> LD{Download Requested?}
    LD -->|Yes| BG[Background Task]
    LD -->|No| I
    BG --> I

    I --> J[Client Response]
    E --> J
```

<!--@auto:diagram:flow:end-->
