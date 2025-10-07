# Sequence Diagram

_Last Updated: 2025-10-07_

## Description

Sequence of interactions for a typical API request, showing component communications.

<!--@auto:diagram:seq:start-->

```mermaid
sequenceDiagram
    participant C as Client
    participant R as FastAPI Router
    participant S as Service Layer
    participant E as External API (Douyin/R2)
    participant D as Data Store

    C->>R: HTTP Request
    R->>S: Route to Service
    S->>E: Fetch Data
    E-->>S: Data Response
    S->>D: Store/Retrieve
    D-->>S: Data
    S-->>R: Processed Response
    R-->>C: JSON Response
```

<!--@auto:diagram:seq:end-->
