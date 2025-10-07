# Control Flow Graph

_Last Updated: 2025-10-07_

## Description

Control flow showing decision points, error paths, and conditional logic in request processing.

<!--@auto:diagram:cfg:start-->

```mermaid
graph TD
    Start[Request Entry] --> Validate[Validate Input]
    Validate -->|Valid| Process[Process Business Logic]
    Validate -->|Invalid| Error400[Return 400 Error]
    Process --> CallExternal[Call External API]
    CallExternal -->|Success| Store[Store Data]
    CallExternal -->|Fail| Error500[Return 500 Error]
    Store -->|Success| Respond[Format Response]
    Store -->|Fail| Error500
    Respond --> End[Return Response]
    Error400 --> End
    Error500 --> End
    End --> Logging[Log with Correlation ID]
```

<!--@auto:diagram:cfg:end-->
