# Sequence Diagram

_Last Updated: 2026-05-06_

## Description

Sequence of interactions for livestream download, post bulk download, and standard API requests, showing component communications.

<!--@auto:diagram:seq:start-->

```mermaid
sequenceDiagram
    participant C as Client
    participant R as FastAPI Router
    participant LS as LivestreamService
    participant PS as PostService
    participant US as UserService
    participant OP as OperationStore
    participant F2 as f2 / Douyin API
    participant DL as DouyinDownloader

    C->>R: POST /livestreams/stream:download
    R->>LS: download_stream(url)
    LS->>LS: _parse_url(url)
    LS->>F2: WebCastIdFetcher.get_webcast_id()
    F2-->>LS: webcast_id

    alt User Profile URL
        LS->>US: get_user_info(user_id)
        US->>F2: fetch user profile
        F2-->>US: profile data
        US-->>LS: UserProfile
        LS->>LS: _stream_map_from_room_data()
    end

    LS->>F2: fetch_user_live_videos(webcast_id)
    F2-->>LS: live_filter
    LS->>LS: _resolve_streams()
    LS->>LS: _select_stream_url()
    LS->>OP: create operation
    LS->>DL: create_task(_run_stream_download)
    LS-->>R: OperationResponse
    R-->>C: 202 {operation_id: "...", status: "pending"}

    Note over DL: Background download runs asynchronously
    DL->>F2: create_stream_tasks()
    F2-->>DL: stream segments
    DL->>OP: update status / progress / result

    C->>R: POST /posts/users/{user_id}/posts:download
    R->>PS: start_bulk_download(user_id)
    PS->>F2: fetch user profile
    F2-->>PS: profile data
    PS->>OP: create operation
    PS-->>R: BulkDownloadResponse
    R-->>C: 202 {operation_id: "...", status: "pending"}

    Note over PS: Background post pagination and downloads run asynchronously
    PS->>F2: fetch posts page
    F2-->>PS: aweme_list + pagination cursor
    PS->>DL: create_download_tasks()
    PS->>OP: update status / progress / per-PostType download_stats
    C->>R: GET /posts/operations/{operation_id}
    R->>PS: get_bulk_download_status(operation_id)
    PS->>OP: get operation
    OP-->>PS: operation metadata with download_stats
    PS-->>R: BulkDownloadResponse
    R-->>C: current totals and per-PostType counts
```

<!--@auto:diagram:seq:end-->
