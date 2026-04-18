# Call Graph

_Last Updated: 2026-04-17_

## Description

Function and method call relationships within the codebase, with detailed coverage of the livestream download pipeline.

<!--@auto:diagram:call:start-->

```mermaid
graph TD
    Main[main:app] --> RouterL[routers.livestreams]
    Main --> RouterP[routers.posts]
    Main --> RouterU[routers.users]

    RouterL --> DL[LivestreamService.download_stream]
    RouterL --> GRI[LivestreamService.get_room_info]
    RouterL --> GDS[LivestreamService.get_download_status]

    DL --> OPCreate[OperationStore.create_operation]
    DL --> PU[_parse_url]
    DL --> RWI[_resolve_webcast_id]
    DL --> LLF[_load_live_filter]
    DL --> RS[_resolve_streams]
    DL --> SSU[_select_stream_url]
    DL --> RSD[_run_stream_download]

    RWI --> WCF[WebCastIdFetcher.get_webcast_id]
    RWI --> RFP[_resolve_from_profile]
    RFP --> GUI[UserService.get_user_info]
    RFP --> SMRD[_stream_map_from_room_data]

    LLF --> FULV[handler.fetch_user_live_videos]
    LLF --> WCF
    LLF --> FRID[handler.fetch_user_live_videos_by_room_id]

    GRI --> LLF
    GRI --> LFTD[_live_filter_to_dict]
    GRI --> ESM[_extract_stream_map]

    RSD --> DDL[DouyinDownloader.create_stream_tasks]
    RSD --> OPUpdate[OperationStore.update_operation]

    RouterP --> SvcP[PostService]
    SvcP --> SchP[schemas.posts]
    SvcP --> ExtAPI[httpx / Douyin API]

    RouterU --> SvcU[UserService]
    SvcU --> OPStore[OperationStore]
    SvcU --> Storage[R2StorageService]
    Storage --> Boto3[boto3.client]

    Main --> CoreDeps[core.dependencies.get_service_container]
    CoreDeps --> Settings[core.settings.get_settings]
```

<!--@auto:diagram:call:end-->
