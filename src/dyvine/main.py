"""Main FastAPI application for Dyvine - Douyin Content Management API.

This module serves as the entry point for the Dyvine application, a high-performance
FastAPI-based REST API for interacting with Douyin (TikTok) content. It provides
comprehensive functionality for downloading, managing, and analyzing Douyin content
including videos, images, live streams, and user data.

Application Architecture:
    The application follows a layered architecture pattern:
    - Presentation Layer: FastAPI routers and endpoints
    - Business Logic Layer: Service classes with domain logic
    - Data Access Layer: External API integration (Douyin, R2 storage)
    - Cross-cutting Concerns: Logging, error handling, dependency injection

Key Features:
    - RESTful API endpoints for Douyin content management
    - Asynchronous request processing with connection pooling
    - Comprehensive error handling with structured responses
    - Request correlation tracking and structured logging
    - Cloudflare R2 integration for content storage
    - Health monitoring and metrics collection
    - CORS support for web browser integration
    - Production-ready deployment configuration

Middleware Stack:
    1. CORS middleware for cross-origin request handling
    2. Request correlation middleware for tracking
    3. Error handling middleware for unified responses
    4. Custom logging middleware for structured logs

Service Dependencies:
    - DouyinHandler: Core integration with Douyin platform
    - UserService: User profile and content operations
    - StorageService: Cloudflare R2 storage integration
    - LoggingService: Structured logging with correlation IDs

Environment Configuration:
    The application uses environment-based configuration:
    - API_DEBUG: Enable debug mode and verbose logging
    - DOUYIN_COOKIE: Authentication cookie for Douyin API
    - R2_* variables: Cloudflare R2 storage configuration
    - SECURITY_* variables: API keys and security settings

Example Usage:
    Start the development server:
        uvicorn src.dyvine.main:app --reload --host 0.0.0.0 --port 8000

    Production deployment:
        gunicorn src.dyvine.main:app -w 4 -k uvicorn.workers.UvicornWorker

    Health check:
        curl http://localhost:8000/health

    API documentation:
        Open http://localhost:8000/docs for Swagger UI
        Open http://localhost:8000/redoc for ReDoc documentation
"""

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .core.dependencies import get_service_container
from .core.error_handlers import register_error_handlers
from .core.logging import ContextLogger, setup_logging
from .core.settings import settings
from .routers import livestreams, posts, users


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI application lifespan context manager.

    Manages the complete lifecycle of the Dyvine application including:
    - Startup initialization (logging, services, dependencies)
    - Runtime state management
    - Graceful shutdown procedures
    - Resource cleanup and finalization

    This context manager ensures proper initialization order and handles
    both successful startup/shutdown and error scenarios during application
    lifecycle events.

    Startup Sequence:
        1. Initialize structured logging system
        2. Create and configure logger instance
        3. Record application start time for uptime tracking
        4. Initialize service container with all dependencies
        5. Log successful startup with environment information

    Shutdown Sequence:
        1. Log shutdown initiation with uptime statistics
        2. Allow service container cleanup (if implemented)
        3. Flush any pending logs or metrics
        4. Release system resources

    Args:
        app: FastAPI application instance to manage.

    Yields:
        None: Control is yielded to the running application.

    Example:
        This function is automatically called by FastAPI when configured
        as the lifespan handler:

        app = FastAPI(lifespan=lifespan)

    Note:
        Any unhandled exceptions during startup will prevent the application
        from starting. During shutdown, exceptions are logged but don't
        prevent the shutdown process.
    """
    # === STARTUP PHASE ===
    setup_logging()
    app.state.logger = ContextLogger(__name__)
    app.state.start_time = time.time()

    # Initialize service container with all dependencies
    container = get_service_container()
    container.initialize()
    app.state.container = container

    # Log successful startup with environment context
    app.state.logger.info(
        "Dyvine application started successfully",
        extra={
            "environment": "development" if settings.debug else "production",
            "version": settings.version,
            "debug_mode": settings.debug,
            "api_prefix": settings.prefix,
            "startup_time": time.time() - app.state.start_time,
        },
    )

    # === RUNTIME PHASE ===
    # Yield control to the running application
    yield

    # === SHUTDOWN PHASE ===
    shutdown_start = time.time()
    total_uptime = shutdown_start - app.state.start_time

    app.state.logger.info(
        "Dyvine application shutting down gracefully",
        extra={
            "total_uptime_seconds": round(total_uptime, 2),
            "total_uptime_hours": round(total_uptime / 3600, 2),
            "shutdown_initiated_at": shutdown_start,
        },
    )


# Create FastAPI application instance with comprehensive configuration
app = FastAPI(
    title=settings.project_name,
    description="""
    Dyvine is a high-performance REST API for Douyin (TikTok) content management.

    Features:
    • Download videos, images, and live streams
    • User profile and content analysis
    • Bulk content operations with progress tracking
    • Cloudflare R2 storage integration
    • Real-time operation monitoring

    Authentication:
    Configure DOUYIN_COOKIE environment variable with valid session data.

    Rate Limits:
    • General endpoints: 10 requests/second
    • Download operations: 2 concurrent per user
    • Content listing: 100 items per request maximum
    """,
    version=settings.version,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url=(
        f"{settings.prefix}/openapi.json" if settings.prefix else "/openapi.json"
    ),
    lifespan=lifespan,
    # Additional metadata for OpenAPI documentation
    contact={
        "name": "Dyvine API Support",
        "email": "billduke@memenow.xyz",
        "url": "https://github.com/memenow/dyvine",
    },
    license_info={
        "name": "Apache 2.0",
        "url": "https://www.apache.org/licenses/LICENSE-2.0.html",
    },
    terms_of_service="https://github.com/memenow/dyvine/blob/main/LICENSE",
)


# Configure CORS middleware for cross-origin request handling
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,  # Configurable via CORS_ORIGINS env var
    allow_credentials=True,  # Allow cookies and auth headers
    allow_methods=["*"],  # Allow all HTTP methods
    allow_headers=["*"],  # Allow all headers
    expose_headers=["X-Correlation-ID"],  # Expose correlation ID to clients
    max_age=3600,  # Cache preflight responses for 1 hour
)


@app.middleware("http")
async def request_middleware(request: Request, call_next: Any) -> Any:
    """HTTP request correlation and logging middleware.

    This middleware provides comprehensive request tracking and logging for all
    HTTP requests processed by the application. It generates unique correlation
    IDs for request tracing, measures request duration, and logs request/response
    metadata for monitoring and debugging purposes.

    Features:
        - Generates unique correlation ID for each request
        - Logs request start with method, path, and client information
        - Measures and logs request processing duration
        - Adds correlation ID to response headers for client tracking
        - Handles both successful and failed request scenarios

    Request Flow:
        1. Generate unique UUID4 correlation ID
        2. Attach correlation ID to request state and logger context
        3. Log request initiation with metadata
        4. Process request through application stack
        5. Measure total processing time
        6. Log request completion with performance metrics
        7. Add correlation header to response

    Args:
        request: Incoming HTTP request object with headers and metadata.
        call_next: Next middleware or route handler in the processing chain.

    Returns:
        HTTP response object with added correlation ID header and timing data.

    Headers Added:
        X-Correlation-ID: Unique request identifier for tracing and debugging.

    Logging Context:
        All log entries within the request include:
        - correlation_id: Unique request identifier
        - method: HTTP method (GET, POST, etc.)
        - path: Request URL path
        - client: Client IP address
        - status_code: HTTP response status
        - duration_ms: Request processing time in milliseconds

    Example:
        Request logs:
        ```
        INFO: Request started {
            "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
            "method": "GET",
            "path": "/api/v1/users/123",
            "client": "192.168.1.100"
        }

        INFO: Request completed {
            "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
            "status_code": 200,
            "duration_ms": 245.67
        }
        ```
    """
    # Determine correlation ID from header or generate a new UUID
    client_request_id = request.headers.get("X-Request-ID")
    correlation_id: str
    request_id_source = "generated"
    if client_request_id:
        try:
            correlation_id = str(uuid.UUID(client_request_id))
            request_id_source = "client"
        except ValueError:
            correlation_id = str(uuid.uuid4())
            request_id_source = "regenerated"
    else:
        correlation_id = str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    request.state.request_id_source = request_id_source

    # Configure logger with correlation context
    logger = app.state.logger
    logger.set_correlation_id(correlation_id)

    # Record request start time for duration measurement
    start_time = time.time()

    # Log request initiation with metadata
    logger.info(
        "HTTP request initiated",
        extra={
            "method": request.method,
            "path": request.url.path,
            "query_params": str(request.query_params) if request.query_params else None,
            "client_ip": request.client.host if request.client else "unknown",
            "user_agent": request.headers.get("user-agent"),
            "content_length": request.headers.get("content-length"),
            "request_id_source": request_id_source,
        },
    )

    # Process request through application stack
    response = await call_next(request)

    # Calculate total processing duration
    duration = time.time() - start_time

    # Log request completion with performance metrics
    logger.info(
        "HTTP request completed",
        extra={
            "status_code": response.status_code,
            "duration_ms": round(duration * 1000, 2),
            "content_length": response.headers.get("content-length"),
            "cache_status": response.headers.get("cache-control"),
            "request_id_source": request_id_source,
        },
    )

    # Add correlation ID to response headers for client tracing
    response.headers["X-Correlation-ID"] = correlation_id
    logger.correlation_id = None

    return response


# Register error handlers
register_error_handlers(app)

# Include routers
app.include_router(posts.router, prefix=settings.prefix)

app.include_router(users.router, prefix=settings.prefix)

app.include_router(livestreams.router, prefix=settings.prefix)


@app.get(
    "/",
    summary="API root information",
    description="Returns basic API information and navigation links",
    response_description="API metadata and documentation links",
    tags=["System"],
)
async def root() -> dict[str, Any]:
    """API root endpoint providing basic service information and navigation.

    This endpoint serves as the entry point for the Dyvine API, providing
    essential information about the service including version, name, and
    links to interactive documentation.

    Returns:
        Dict[str, Any]: Service metadata including:
            - name: Human-readable API name
            - version: Current API version (semantic versioning)
            - docs: URL path to Swagger/OpenAPI documentation
            - redoc: URL path to ReDoc documentation
            - status: Current operational status string
            - api_prefix: Configured API prefix
            - features: List of available feature domains

    Example:
        ```bash
        curl -X GET "https://api.example.com/"
        ```

        Response:
        ```json
        {
            "name": "Dyvine API",
            "version": "1.0.0",
            "docs": "/docs",
            "redoc": "/redoc",
            "status": "operational",
            "api_prefix": "/api/v1",
            "features": ["users", "posts", "livestreams"]
        }
        ```

    Note:
        This endpoint is always available and doesn't require authentication.
        It's commonly used for service discovery and API health verification.
    """
    return {
        "name": settings.project_name,
        "version": settings.version,
        "docs": "/docs",
        "redoc": "/redoc",
        "status": "operational",
        "api_prefix": settings.prefix,
        "features": ["users", "posts", "livestreams"],
    }


@app.get(
    "/health",
    summary="Application health check",
    description="Returns detailed health and performance metrics for monitoring",
    response_description="Health status with system metrics and uptime information",
    tags=["System"],
)
async def health_check(request: Request) -> JSONResponse:
    """Comprehensive health check endpoint for monitoring and diagnostics.

    This endpoint provides detailed health information about the Dyvine API
    including system metrics, resource usage, and operational status. It's
    designed for use by monitoring systems, load balancers, and operational
    dashboards.

    Health Metrics Included:
        - Application status and version
        - System uptime since last restart
        - Memory usage (RSS) in megabytes
        - CPU utilization percentage
        - Service dependencies status

    Returns:
        Dict[str, Any]: Comprehensive health information including:
            - status: Overall health status ("healthy", "degraded", "unhealthy")
            - version: Current application version
            - uptime_seconds: Seconds since application startup
            - uptime_human: Human-readable uptime string
            - memory_mb: Current memory usage in MB
            - cpu_percent: Current CPU usage percentage
            - dependencies: Status of external dependencies
            - correlation_id: Correlated request identifier for tracking

    Status Codes:
        - 200: Service is healthy and operational
        - 503: Service is degraded or unhealthy

    Example:
        ```bash
        curl -X GET "https://api.example.com/health"
        ```

        Response:
        ```json
        {
            "status": "healthy",
            "version": "1.0.0",
            "uptime_seconds": 3600,
            "uptime_human": "1 hour, 0 minutes",
            "memory_mb": 245.67,
            "cpu_percent": 12.5,
            "dependencies": {
                "douyin_api": "connected",
                "r2_storage": "available"
            },
            "correlation_id": "550e8400-e29b-41d4-a716-446655440000"
        }
        ```

    Note:
        This endpoint is used by:
        - Kubernetes liveness and readiness probes
        - Load balancer health checks
        - Monitoring systems (Prometheus, Grafana, etc.)
        - Operational dashboards and alerting
    """
    import psutil

    # Get current process information
    process = psutil.Process()
    uptime_seconds = int(time.time() - app.state.start_time)

    # Calculate human-readable uptime
    hours = uptime_seconds // 3600
    minutes = (uptime_seconds % 3600) // 60
    uptime_human = f"{hours} hours, {minutes} minutes"

    # Check dependency status (can be expanded for real health checks)
    dependencies = {
        "douyin_api": "configured" if settings.douyin.cookie else "missing_credentials",
        "r2_storage": "available" if settings.r2.is_configured else "not_configured",
        "logging_system": "operational",
    }

    # Determine overall health status
    health_status = "healthy"
    if not settings.douyin.cookie:
        health_status = "unhealthy"
    elif not settings.r2.is_configured:
        health_status = "degraded"

    if process.memory_info().rss > 1024 * 1024 * 1024:  # > 1GB RAM
        if health_status != "unhealthy":
            health_status = "degraded"

    correlation_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))

    response_body = {
        "status": health_status,
        "version": settings.version,
        "environment": "development" if settings.debug else "production",
        "uptime_seconds": uptime_seconds,
        "uptime_human": uptime_human,
        "memory_mb": round(process.memory_info().rss / 1024 / 1024, 2),
        "cpu_percent": round(process.cpu_percent(), 2),
        "dependencies": dependencies,
        "timestamp": time.time(),
        "api_prefix": settings.prefix,
        "correlation_id": correlation_id,
    }

    status_code = (
        status.HTTP_200_OK
        if health_status == "healthy"
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )

    return JSONResponse(status_code=status_code, content=response_body)
