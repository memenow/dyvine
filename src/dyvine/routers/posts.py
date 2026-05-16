"""Post-facing FastAPI router.

Endpoints exposed under ``/api/v1/posts``:

- ``GET /{post_id}`` — return the materialised ``PostDetail`` for a
  Douyin ``aweme_id``.
- ``GET /users/{user_id}/posts`` — paginated list of a user's posts.
  Pagination uses an opaque ``page_token`` derived from the upstream
  Douyin cursor; clients must echo ``next_page_token`` back unchanged
  on follow-up calls.
- ``POST /users/{user_id}/posts:download`` — schedule an asynchronous
  bulk download of every available post. Returns ``202`` with an
  ``operation_id`` that ``get_bulk_download_operation`` polls for
  progress, including per-``PostType`` counters.
- ``GET /operations/{operation_id}`` — bulk-download status snapshot.

Authentication is enforced via the router-level ``require_api_key``
dependency. Static routes (``/operations/...`` and ``/users/...``) are
registered before the catch-all ``/{post_id}`` so FastAPI's order-aware
matcher selects the dedicated handlers instead of treating
``operations`` or ``users`` as a literal post ID.
"""

import base64
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status

from ..core.decorators import handle_errors
from ..core.dependencies import get_post_service, require_api_key
from ..core.logging import ContextLogger
from ..schemas.posts import BulkDownloadResponse, ListPostsResponse, PostDetail
from ..services.posts import PostService

# Initialize logger for this module
logger = ContextLogger(__name__)

_USER_ID_PATTERN = r"^[A-Za-z0-9_\-]{6,128}$"
_POST_ID_PATTERN = r"^[0-9]{6,32}$"
_OPERATION_ID_PATTERN = r"^[A-Za-z0-9_\-]{8,128}$"

# Create router with posts prefix and OpenAPI tags
router = APIRouter(
    prefix="/posts",
    tags=["posts"],
    dependencies=[Depends(require_api_key)],
    responses={
        401: {"description": "Missing or invalid API key"},
        404: {"description": "Post or user not found"},
        422: {"description": "Validation error"},
        500: {"description": "Internal server error"},
    },
)


# Static-segment routes (``/operations/...``, ``/users/...``) must be
# registered before the catch-all ``/{post_id}`` so FastAPI's order-aware
# matcher routes them to the dedicated handlers instead of treating
# ``operations`` or ``users`` as a literal post id.


@router.get(
    "/operations/{operation_id}",
    response_model=BulkDownloadResponse,
    summary="Get bulk download operation status",
    description=(
        "Retrieves the current status of a bulk posts download operation, "
        "including per-PostType counts and the destination download path "
        "once available"
    ),
)
@handle_errors(logger=logger)
async def get_bulk_download_operation(
    service: Annotated[PostService, Depends(get_post_service)],
    operation_id: str = Path(
        ...,
        pattern=_OPERATION_ID_PATTERN,
        description="The unique identifier of the bulk download operation",
    ),
) -> BulkDownloadResponse:
    """Retrieve the status of a bulk posts download operation."""
    logger.info(
        "Processing get_bulk_download_operation request",
        extra={"operation_id": operation_id},
    )
    return await service.get_bulk_download_status(operation_id)


@router.get(
    "/users/{user_id}/posts",
    response_model=ListPostsResponse,
    summary="List posts from a specific user with pagination",
    description=(
        "Retrieves a paginated list of posts from a specific Douyin user, "
        "ordered by creation time"
    ),
    response_description="Paginated list of post details with opaque next-page token",
)
@handle_errors(logger=logger)
async def list_user_posts(
    service: Annotated[PostService, Depends(get_post_service)],
    user_id: str = Path(
        ...,
        pattern=_USER_ID_PATTERN,
        description="Unique Douyin user identifier (sec_user_id)",
    ),
    page_token: str | None = Query(
        None,
        description=(
            "Opaque pagination token returned by a previous response. "
            "Omit on the first request."
        ),
    ),
    count: int = Query(
        20,
        ge=1,
        le=100,
        description="Number of posts to return per page. Must be between 1 and 100",
    ),
) -> ListPostsResponse:
    """Retrieve a paginated list of posts from a specific Douyin user."""
    cursor = _decode_page_token(page_token)
    logger.info(
        "Processing list_user_posts request",
        extra={"user_id": user_id, "page_token": page_token, "count": count},
    )
    page = await service.get_user_posts(user_id, cursor, count)
    # The upstream Douyin ``max_cursor`` is an opaque sentinel — not an
    # offset — so the next page token must echo it back verbatim.
    # Synthesising a token from ``cursor + len(posts)`` would resolve to
    # a window the upstream API does not recognise, repeating or
    # skipping posts.
    next_token = (
        _encode_page_token(page.next_cursor) if page.next_cursor is not None else None
    )
    return ListPostsResponse(
        posts=page.posts, next_page_token=next_token, total_size=None
    )


@router.post(
    "/users/{user_id}/posts:download",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=BulkDownloadResponse,
    summary="Schedule user posts bulk download",
    description=(
        "Schedules an asynchronous bulk download of every available post from "
        "a specific user and returns an operation_id clients can poll for "
        "progress"
    ),
)
@handle_errors(logger=logger)
async def download_user_posts(
    service: Annotated[PostService, Depends(get_post_service)],
    user_id: str = Path(
        ...,
        pattern=_USER_ID_PATTERN,
        description="The unique identifier of the user",
    ),
    page_token: str | None = Query(
        None,
        description=(
            "Optional starting cursor token. Omit to begin at the newest " "post."
        ),
    ),
) -> BulkDownloadResponse:
    """Schedule a bulk download of every available post from a user."""
    cursor = _decode_page_token(page_token)
    logger.info(
        "Processing download_user_posts request",
        extra={"user_id": user_id, "page_token": page_token},
    )
    return await service.start_bulk_download(user_id, cursor)


@router.get(
    "/{post_id}",
    response_model=PostDetail,
    summary="Get detailed information about a specific post",
    description=(
        "Retrieves comprehensive details about a Douyin post including metadata, "
        "media URLs, and engagement statistics"
    ),
    response_description="Complete post information with media details and statistics",
)
@handle_errors(logger=logger)
async def get_post(
    service: Annotated[PostService, Depends(get_post_service)],
    post_id: str = Path(
        ...,
        pattern=_POST_ID_PATTERN,
        description="Unique Douyin post identifier (aweme_id)",
    ),
) -> PostDetail:
    """Retrieve detailed information about a specific Douyin post."""
    logger.info(
        "Fetching post details",
        extra={"post_id": post_id, "operation": "get_post_detail"},
    )
    return await service.get_post_detail(post_id)


def _encode_page_token(cursor: int) -> str:
    """Render a numeric cursor as an opaque base64 token."""
    payload = str(int(cursor)).encode("ascii")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_page_token(token: str | None) -> int:
    """Decode an opaque page token back into the numeric upstream cursor.

    Invalid tokens fall back to ``0`` so a client retry with corrupted
    state still serves the first page rather than failing the whole
    request. ``ValidationError`` is intentionally not raised because the
    cursor is opaque to clients and they cannot debug a structured
    rejection.
    """
    if not token:
        return 0
    try:
        padding = "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(token + padding).decode("ascii")
        return max(int(decoded), 0)
    except (ValueError, UnicodeDecodeError):
        return 0
