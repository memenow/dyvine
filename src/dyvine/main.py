"""Main FastAPI application module.

Initializes and configures the FastAPI application, handling:
- API settings and metadata
- CORS middleware
- Logging
- Router registration
- Exception handlers
- Startup and shutdown events
"""

import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict

from f2.apps.douyin.handler import DouyinHandler
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .core.logging import ContextLogger, setup_logging
from .core.settings import settings
from .routers import livestreams, posts, users
from .services.livestreams import DownloadError as LivestreamDownloadError
from .services.posts import DownloadError as PostDownloadError
from .services.posts import PostServiceError
from .services.users import DownloadError as UserDownloadError
from .services.users import UserService, UserServiceError

# Don't initialize logger here
# logger = ContextLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifespan event handler for application startup and shutdown.

    This function performs the following tasks during the application's lifespan:

    Startup:
    - Initializes logging and the ContextLogger.
    - Verifies required configuration settings (e.g., DOUYIN_COOKIE in production).
    - Sets up monitoring and global instances (DouyinHandler and UserService).
    - Logs application startup information.

    Shutdown:
    - Performs cleanup tasks (add any resource cleanup here).
    - Logs application shutdown information, including uptime.

    Args:
        app: The FastAPI application instance.
    """
    # Initialize logging and ContextLogger here
    setup_logging()
    import logging
    logger = ContextLogger(logging.getLogger(__name__))
    app.state.logger = logger # Store logger in app state

    # Verify required settings
    if not settings.debug:
        if not settings.douyin_cookie:
            raise ValueError("DOUYIN_COOKIE must be set")
        if settings.api_key == "default-api-key-please-change-in-production":
            raise ValueError(
                "Default API key detected! Set API_KEY in your .env file "
                "for production use."
            )
    else:
        import warnings
        if settings.api_key == "default-api-key-please-change-in-production":
            warnings.warn(
                "Using default API key. This is okay for development but "
                "must be changed in production."
            )
        if not settings.douyin_cookie:
            warnings.warn(
                "Douyin cookie not set. Some features may not work properly."
            )

    # Initialize Douyin handler and UserService
    handler_kwargs = {
        "headers": settings.douyin_headers,
        "proxies": settings.douyin_proxies,
        "mode": "all",
        "cookie": settings.douyin_cookie,
        "path": "downloads",
        "max_retries": 5,
        "timeout": 30,
        "chunk_size": 1024 * 1024,
        "max_tasks": 3,
        "folderize": True,
        "download_image": True,
        "download_video": True,
        "download_live": True,
        "download_collection": True,
        "download_story": True,
        "naming": "{create}_{desc}",
        "page_counts": 100,
    }
    app.state.douyin_handler = DouyinHandler(handler_kwargs)
    app.state.user_service = UserService()

    logger.info(
        "Application starting",
        extra={
            "environment": "development" if settings.debug else "production",
            "version": settings.version,
            "host": settings.host,
            "port": settings.port,
            "debug": settings.debug,
        },
    )
    yield
    # Add cleanup tasks here
    logger.info(
      "Application shutting down",
      extra={"uptime_seconds": time.time() - app.state.start_time}
    )

# Create FastAPI application
app = FastAPI(
    title=settings.project_name,
    description="API for interacting with Douyin content",
    version=settings.version,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

# Store application start time
app.state.start_time = time.time()

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def request_middleware(request: Request, call_next):
    """Middleware for request tracking, logging, and performance monitoring.

    This middleware adds the following features:
    - Request correlation IDs (added to logs and response headers)
    - Performance metrics (request duration)
    - Memory tracking (memory usage during request processing)
    - Error tracking (logs exceptions with details)
    - Request/response logging (logs incoming requests and responses)

    Args:
        request: Incoming HTTP request.
        call_next: Next middleware in the chain.

    Returns:
        Response: Response from the next middleware or the endpoint.
    """
    correlation_id = str(uuid.uuid4())
    logger = app.state.logger
    logger.set_correlation_id(correlation_id)

    # Log and track request
    async with logger.track_time("request"):
        logger.info(
            "Incoming request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "query_params": str(request.query_params),
                "client_host": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent"),
            }
        )

        try:
            # Track memory usage during request processing
            async with logger.track_memory("request_processing"):
                response = await call_next(request)

            # Log response
            logger.info(
                "Request completed",
                extra={
                    "status_code": response.status_code,
                    "response_headers": dict(response.headers),
                }
            )

            # Add tracking headers
            response.headers["X-Correlation-ID"] = correlation_id
            return response

        except Exception as e:
            logger.exception(
                "Request failed",
                extra={
                    "error_type": e.__class__.__name__,
                    "error_details": str(e),
                    "traceback": True
                }
            )
            return JSONResponse(
                status_code=500,
                content={
                    "detail": "Internal server error",
                    "correlation_id": correlation_id,
                    "error_type": type(e).__name__
                }
            )


@app.exception_handler(UserServiceError)
async def user_service_exception_handler(
    request: Request, exc: UserServiceError
) -> JSONResponse:
    """Exception handler for UserService-specific exceptions.

    Args:
        request: Request that caused the exception.
        exc: Raised exception.

    Returns:
        JSONResponse: JSON response with error details.
    """
    logger = app.state.logger
    logger.error(
        "UserService error",
        extra={
            "error": str(exc),
            "error_type": exc.__class__.__name__,
            "path": request.url.path,
        }
    )
    return JSONResponse(
        status_code=400,
        content={"detail": str(exc)},
    )

@app.exception_handler(PostServiceError)
async def post_service_exception_handler(
    request: Request, exc: PostServiceError
) -> JSONResponse:
    """Exception handler for PostService-specific exceptions.

    Args:
        request: Request that caused the exception.
        exc: Raised exception.

    Returns:
        JSONResponse: JSON response with error details.
    """
    logger = app.state.logger
    logger.error(
        "PostService error",
        extra={
            "error": str(exc),
            "error_type": exc.__class__.__name__,
            "path": request.url.path
        }
    )
    return JSONResponse(
        status_code=400,
        content={"detail": str(exc)},
    )

@app.exception_handler(LivestreamDownloadError)
@app.exception_handler(PostDownloadError)
@app.exception_handler(UserDownloadError)
async def download_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Exception handler for DownloadError exceptions.

    Note: Since DownloadError is defined in multiple services,
    we cannot determine the specific service from the exception type alone.
    """
    logger = app.state.logger
    logger.error(
        "Download error",
        extra={
            "error": str(exc),
            "error_type": exc.__class__.__name__,
            "path": request.url.path,
        },
    )

    return JSONResponse(
        status_code=500,
        content={"detail": "Download failed", "error_details": str(exc)},
    )

@app.exception_handler(Exception)  # Catch-all exception handler
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Generic exception handler for all unhandled exceptions.
    """
    logger = app.state.logger
    logger.exception(
        "Unhandled exception",
        extra={
            "error_type": exc.__class__.__name__,
            "error_details": str(exc),
            "path": request.url.path,
        },
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "error_type": type(exc).__name__,
            "error_details": str(exc)
        }
    )

# Include routers
app.include_router(
    posts.router,
    prefix=settings.prefix
)

app.include_router(
    users.router,
    prefix=settings.prefix
)

app.include_router(
    livestreams.router,
    prefix=settings.prefix
)

@app.get("/")
async def root() -> Dict[str, str]:
    """Root endpoint.

    Returns:
        Dict: Basic API information.
    """
    return {
        "name": settings.project_name,
        "version": settings.version,
        "docs": "/docs",
        "redoc": "/redoc"
    }

@app.get("/health")
async def health_check() -> Dict[str, Any]:
    """Health check endpoint.

    Returns:
        Dict: Detailed system health information including:
            - Application status
            - Version
            - Uptime
            - Memory usage
            - System load
    """
    import psutil
    process = psutil.Process()
    
    return {
        "status": "healthy",
        "version": settings.version,
        "uptime_seconds": int(time.time() - app.state.start_time),
        "memory": {
            "used_mb": round(process.memory_info().rss / 1024 / 1024, 2),
            "percent": process.memory_percent()
        },
        "cpu": {
            "percent": process.cpu_percent(),
            "threads": process.num_threads()
        },
        "system": {
            "cpu_percent": psutil.cpu_percent(),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": psutil.disk_usage('/').percent
        }
    }
