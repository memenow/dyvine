"""FastAPI entry point for Dyvine.

Wires the Dyvine service: lifespan-driven `ServiceContainer` initialisation,
the CORS + correlation-ID HTTP middleware, three feature routers
(`users`, `posts`, `livestreams`), and the operational endpoints
(`/livez`, `/readyz`, `/startupz`, `/health`, plus the Prometheus
metrics ASGI app at `/metrics`).

Architecture:
    - Presentation: FastAPI routers under `routers/` (each gated by the
      `require_api_key` dependency mounted at the router level).
    - Service: `UserService`, `PostService`, `LivestreamService`,
      `R2StorageService` constructed by `ServiceContainer`.
    - Persistence: `OperationStore` (SQLite + WAL) accessed through a
      dedicated `sqlite_executor`; long-running tasks are tracked by a
      shared `BackgroundTaskRegistry`.
    - Observability: structured JSON logging with contextvars-based
      correlation IDs, Prometheus counters/histograms.

Middleware:
    1. `CORSMiddleware` honours `API_CORS_ORIGINS`; credentialed CORS is
       auto-disabled when the allowlist is `["*"]`.
    2. `request_middleware` assigns a UUID4 correlation ID per request
       (or accepts a UUID provided via `X-Request-ID`), measures
       duration, and exposes the ID via `X-Correlation-ID`.
    Exception handlers registered through `register_error_handlers`
    translate `DyvineError` subclasses and `HTTPException` into a
    single error envelope; they are not middleware.

Environment configuration:
    `API_*`, `SECURITY_*`, `DOUYIN_*`, and `R2_*` variables drive
    `core.settings.Settings`. The composite validator refuses to boot
    when `API_DEBUG=false` and either `SECURITY_SECRET_KEY` or
    `SECURITY_API_KEY` (when `SECURITY_REQUIRE_API_KEY` is true) still
    matches the placeholder sentinel.

Examples:
    Local development::

        uv run uvicorn src.dyvine.main:app --reload

    Production-style::

        uv run uvicorn src.dyvine.main:app --host 0.0.0.0 --port 8000 \\
            --timeout-graceful-shutdown 25

    Multi-worker deployments are unsafe today: the default
    `OperationStore` writes to a pod-local SQLite file, so scaling
    requires either replacing that backend with a shared store or
    pinning the deployment to a single replica (see the Kustomize
    base, which uses `Recreate` + `replicas: 1`).
"""

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psutil
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram, make_asgi_app

from .core.dependencies import get_service_container
from .core.error_handlers import register_error_handlers
from .core.logging import ContextLogger, setup_logging
from .core.settings import settings
from .routers import livestreams, posts, users

http_requests_total = Counter(
    "dyvine_http_requests_total",
    "Total HTTP requests handled by Dyvine",
    ["method", "route", "status_code"],
)
http_request_duration_seconds = Histogram(
    "dyvine_http_request_duration_seconds",
    "HTTP request duration for Dyvine",
    ["method", "route", "status_code"],
)


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
        3. Record monotonic startup baseline for uptime tracking
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
    # Use a monotonic baseline so uptime measurements stay correct across
    # NTP step adjustments and any wall-clock skew. Wall-clock timestamps
    # (for log records and the ``timestamp`` field in ``/health``) still go
    # through ``time.time()`` where they are needed.
    app.state.start_monotonic = time.monotonic()

    # Initialize service container with all dependencies
    container = get_service_container()
    await container.initialize()
    app.state.container = container
    app.state.startup_complete = True

    # Log successful startup with environment context
    app.state.logger.info(
        "Dyvine application started successfully",
        extra={
            "environment": "development" if settings.debug else "production",
            "version": settings.version,
            "debug_mode": settings.debug,
            "api_prefix": settings.prefix,
            "startup_time": time.monotonic() - app.state.start_monotonic,
        },
    )

    # === RUNTIME PHASE ===
    # Yield control to the running application
    yield

    # === SHUTDOWN PHASE ===
    # ``total_uptime`` is computed from the monotonic baseline so the value
    # is stable even if the system clock jumped during the lifetime of the
    # process. ``shutdown_initiated_at`` keeps a wall-clock stamp because
    # operators correlate it with external log streams.
    total_uptime = time.monotonic() - app.state.start_monotonic
    shutdown_initiated_at = time.time()

    app.state.logger.info(
        "Dyvine application shutting down gracefully",
        extra={
            "total_uptime_seconds": round(total_uptime, 2),
            "total_uptime_hours": round(total_uptime / 3600, 2),
            "shutdown_initiated_at": shutdown_initiated_at,
        },
    )
    app.state.startup_complete = False

    # Drain the dedicated executor pools before the process exits. Missing
    # this step leaves non-daemon worker threads alive and delays shutdown
    # until the Python interpreter tears them down.
    try:
        await container.shutdown()
    except Exception:  # pragma: no cover - shutdown is best-effort
        app.state.logger.exception("Service container shutdown failed")


# Create FastAPI application instance with comprehensive configuration
app = FastAPI(
    title=settings.project_name,
    description="""
    Dyvine is a REST API for Douyin (TikTok) content management.

    Features:
    - Asynchronous downloads of videos, image galleries, and livestreams.
    - User profile lookup and bulk download orchestration.
    - Persistent operation tracking with poll-based progress.
    - Optional Cloudflare R2 archival when every R2 setting is configured.

    Authentication:
    - Application: every router endpoint requires the `X-API-Key`
      header to match `SECURITY_API_KEY` when `SECURITY_REQUIRE_API_KEY`
      is true (the default). Set the variable to false only when the
      API is fronted by another authenticated layer.
    - Upstream: `DOUYIN_COOKIE` must hold a valid Douyin session
      cookie for the f2 SDK to talk to the upstream API.

    Notes:
    - Asynchronous downloads return a persisted operation record;
      poll the matching `/operations/{id}` endpoint for progress.
    - Application-level rate limiting is not enforced; rely on the
      gateway / ingress fronting the deployment.
    - Prometheus metrics are exposed at `/metrics`.
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
allow_all_origins = settings.cors_origins == ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,  # Configurable via CORS_ORIGINS env var
    allow_credentials=not allow_all_origins,
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

    # Record request start time for duration measurement. ``perf_counter``
    # is monotonic and provides the highest available resolution, which is
    # the right primitive for an elapsed-time pair where both endpoints are
    # taken in the same process.
    start_time = time.perf_counter()

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

    # Calculate total processing duration on the same monotonic clock that
    # captured ``start_time`` above.
    duration = time.perf_counter() - start_time

    # Log request completion with performance metrics
    route = request.scope.get("route")
    route_label = getattr(route, "path", None) or "unmatched"
    status_code_label = str(response.status_code)
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
    http_requests_total.labels(
        method=request.method,
        route=route_label,
        status_code=status_code_label,
    ).inc()
    http_request_duration_seconds.labels(
        method=request.method,
        route=route_label,
        status_code=status_code_label,
    ).observe(duration)

    # Add correlation ID to response headers for client tracing
    response.headers["X-Correlation-ID"] = correlation_id
    logger.set_correlation_id(None)
    logger.clear_context()

    return response


# Register error handlers
register_error_handlers(app)

# Include routers
app.include_router(posts.router, prefix=settings.prefix)

app.include_router(users.router, prefix=settings.prefix)

app.include_router(livestreams.router, prefix=settings.prefix)

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)


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
    "/livez",
    summary="Liveness probe",
    description="Returns a process-level liveness signal for container orchestration",
    response_description="Liveness status",
    tags=["System"],
)
async def liveness_probe(request: Request) -> JSONResponse:
    """Return a liveness signal that only reflects process health."""
    correlation_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "live",
            "version": settings.version,
            "correlation_id": correlation_id,
        },
    )


@app.get(
    "/readyz",
    summary="Readiness probe",
    description="Returns a readiness signal based on required dependencies only",
    response_description="Readiness status",
    tags=["System"],
)
async def readiness_probe(request: Request) -> JSONResponse:
    """Return readiness based on required runtime dependencies.

    The probe inspects every dependency required to serve a real request
    and fails fast (HTTP 503) if any of them are missing or unreachable.
    The checks are intentionally conservative: a Pod that cannot accept
    work must not be routed traffic by the orchestrator.
    """
    correlation_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))

    # Douyin API authentication cookie must be configured to reach upstream.
    douyin_ok = bool(settings.douyin.cookie)
    douyin_status = "configured" if douyin_ok else "missing_credentials"

    # Service container must be wired before any request handler runs.
    container = getattr(app.state, "container", None)
    container_ok = container is not None
    container_status = "initialized" if container_ok else "missing"

    # Operation store backs asynchronous workflows; a broken SQLite path
    # makes content downloads fail at the first write.
    operation_store_ok = False
    operation_store_status = "missing"
    if container is not None:
        try:
            await container.operation_store.healthcheck()
            operation_store_ok = True
            operation_store_status = "available"
        except Exception:
            operation_store_ok = False
            operation_store_status = "unavailable"

    # R2 storage credentials, bucket, and endpoint are all required to
    # persist downloaded content. Delegate to ``R2Settings.is_configured``
    # so ``/readyz`` and ``R2StorageService`` stay in lockstep on the
    # exact fields a real upload needs.
    r2_ok = settings.r2.is_configured
    r2_status = "configured" if r2_ok else "missing_credentials"

    ready = douyin_ok and container_ok and operation_store_ok and r2_ok
    readiness_status_code = (
        status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return JSONResponse(
        status_code=readiness_status_code,
        content={
            "status": "ready" if ready else "not_ready",
            "dependencies": {
                "douyin_api": douyin_status,
                "service_container": container_status,
                "operation_store": operation_store_status,
                "r2_storage": r2_status,
            },
            "correlation_id": correlation_id,
        },
    )


@app.get(
    "/startupz",
    summary="Startup probe",
    description="Returns startup completion status for container orchestration",
    response_description="Startup status",
    tags=["System"],
)
async def startup_probe(request: Request) -> JSONResponse:
    """Return whether the application startup sequence completed."""
    correlation_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    started = bool(getattr(app.state, "startup_complete", False))
    startup_status_code = (
        status.HTTP_200_OK if started else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return JSONResponse(
        status_code=startup_status_code,
        content={
            "status": "started" if started else "starting",
            "correlation_id": correlation_id,
        },
    )


@app.get(
    "/health",
    summary="Application health summary",
    description="Returns aggregated runtime metrics for ops dashboards",
    response_description="Aggregated metrics and informational dependency state",
    tags=["System"],
)
async def health_check(request: Request) -> JSONResponse:
    """Aggregate runtime metrics for operational monitoring dashboards.

    ``/health`` is intentionally an informational endpoint: it exists so
    operators, dashboards, and ad-hoc monitoring tools can scrape a single
    URL for uptime, memory, and CPU figures. It always returns ``200 OK``
    and never gates on dependency availability.

    Probe responsibilities are split across dedicated endpoints so each
    Kubernetes probe type maps to a single concern and can be tuned
    independently:

    - Liveness probe: ``GET /livez`` reflects only process health.
    - Readiness probe: ``GET /readyz`` returns ``503`` when a required
      dependency (Douyin cookie, service container, operation store, R2)
      is unavailable so traffic is steered away from a Pod that cannot
      serve real requests.
    - Startup probe: ``GET /startupz`` reports whether the lifespan
      startup sequence finished.

    Returns:
        JSONResponse: The response body always carries ``status == "ok"``
        plus the following fields:
            - version: Current application version.
            - environment: ``"development"`` or ``"production"``.
            - uptime_seconds: Seconds since the lifespan startup hook ran,
              measured on a monotonic clock so NTP adjustments cannot
              produce negative or jumping values.
            - uptime_human: Human-readable rendering of ``uptime_seconds``.
            - memory_mb: Resident memory of the current process in MiB.
            - cpu_percent: Instantaneous CPU usage percentage.
            - timestamp: Wall-clock timestamp (``time.time()``) of the
              snapshot, intended for log correlation.
            - api_prefix: Configured API prefix.
            - correlation_id: Request correlation ID, mirrored to the
              ``X-Correlation-ID`` response header.
            - dependencies: Informational dependency snapshot. Surfaced
              for ops visibility only; values here never change the HTTP
              status code.
            - memory_pressure: ``"high"`` when RSS exceeds 1 GiB, else
              ``"normal"``. Informational only.

    Status Codes:
        - 200: Always.

    Example:
        ```bash
        curl -X GET "https://api.example.com/health"
        ```

        Response:
        ```json
        {
            "status": "ok",
            "version": "1.0.0",
            "uptime_seconds": 3600,
            "uptime_human": "1 hours, 0 minutes",
            "memory_mb": 245.67,
            "cpu_percent": 12.5,
            "memory_pressure": "normal",
            "dependencies": {
                "douyin_api": "configured",
                "r2_storage": "configured",
                "logging_system": "operational"
            },
            "timestamp": 1714723200.123,
            "correlation_id": "550e8400-e29b-41d4-a716-446655440000"
        }
        ```
    """
    # Get current process information
    process = psutil.Process()
    # ``start_monotonic`` is set at the top of the lifespan startup hook,
    # which always runs before any HTTP request. The ``getattr`` fallback
    # protects exotic call paths (e.g. tests instantiating ``app`` without
    # the lifespan) by returning a zero uptime instead of raising.
    start_monotonic = getattr(app.state, "start_monotonic", 0.0)
    uptime_seconds = int(time.monotonic() - start_monotonic) if start_monotonic else 0

    # Calculate human-readable uptime
    hours = uptime_seconds // 3600
    minutes = (uptime_seconds % 3600) // 60
    uptime_human = f"{hours} hours, {minutes} minutes"

    # Informational dependency snapshot. Values here document the current
    # configuration but never affect the HTTP status code. Use ``/readyz``
    # if you need the dependency-aware gate.
    douyin_status = "configured" if settings.douyin.cookie else "missing_credentials"
    r2_status = "configured" if settings.r2.is_configured else "missing_credentials"
    dependencies = {
        "douyin_api": douyin_status,
        "r2_storage": r2_status,
        "logging_system": "operational",
    }

    rss_bytes = process.memory_info().rss
    memory_pressure = "high" if rss_bytes > 1024 * 1024 * 1024 else "normal"

    correlation_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))

    response_body = {
        "status": "ok",
        "version": settings.version,
        "environment": "development" if settings.debug else "production",
        "uptime_seconds": uptime_seconds,
        "uptime_human": uptime_human,
        "memory_mb": round(rss_bytes / 1024 / 1024, 2),
        "cpu_percent": round(process.cpu_percent(), 2),
        "memory_pressure": memory_pressure,
        "dependencies": dependencies,
        "timestamp": time.time(),
        "api_prefix": settings.prefix,
        "correlation_id": correlation_id,
    }

    return JSONResponse(status_code=status.HTTP_200_OK, content=response_body)
