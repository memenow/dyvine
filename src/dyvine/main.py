"""Main FastAPI application module.

This module initializes and configures the FastAPI application. It handles the
following tasks:

- Setting up API settings and metadata
- Configuring CORS middleware
- Initializing logging
- Registering routers
- Defining exception handlers
- Defining startup and shutdown events
"""

from typing import Dict, Any
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .core.settings import settings
from .core.logging import setup_logging, ContextLogger
from .routers import posts, users, livestreams
from .services.posts import PostServiceError

# Initialize logging
setup_logging()
logger = ContextLogger(__name__)

# Create FastAPI application
app = FastAPI(
    title=settings.project_name,
    description="API for interacting with Douyin content",
    version=settings.version,
    docs_url="/docs",
    redoc_url="/redoc"
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
    - Request correlation IDs
    - Performance metrics
    - Memory tracking
    - Error tracking
    - Request/response logging

    Args:
        request (Request): Incoming HTTP request.
        call_next (Callable): Next middleware in the chain.

    Returns:
        Response: Response from the next middleware.
    """
    correlation_id = str(uuid.uuid4())
    logger.set_correlation_id(correlation_id)
    
    # Log and track request
    with logger.track_time("request"):
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
            with logger.track_memory("request_processing"):
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
                    "error_type": e.__class__.__name__
                }
            )

@app.exception_handler(PostServiceError)
async def post_service_exception_handler(
    request: Request, exc: PostServiceError
) -> JSONResponse:
    """Exception handler for PostService-specific exceptions.

    Args:
        request (Request): Request that caused the exception.
        exc (PostServiceError): Raised exception.

    Returns:
        JSONResponse: JSON response with error details.
    """
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
        content={"detail": str(exc)}
    )

@app.on_event("startup")
async def startup_event() -> None:
    """Event handler for application startup.

    This function performs the following tasks:
    - Initialize logging
    - Verify configuration
    - Set up monitoring
    - Log application startup
    """
    with logger.track_time("startup"):
        # Verify required settings
        if not settings.debug:  # Only validate in production mode
            if not settings.douyin_cookie:
                raise ValueError("DOUYIN_COOKIE must be set")
            
        # Log startup info
        logger.info(
            "Application starting",
            extra={
                "environment": "development" if settings.debug else "production",
                "version": settings.version,
                "host": settings.host,
                "port": settings.port,
                "debug": settings.debug,
            }
        )

@app.on_event("shutdown")
async def shutdown_event() -> None:
    """Event handler for application shutdown.

    This function performs the following tasks:
    - Close connections
    - Cleanup resources
    - Log shutdown
    """
    with logger.track_time("shutdown"):
        # Add cleanup tasks here
        logger.info(
            "Application shutting down",
            extra={"uptime_seconds": time.time() - app.state.start_time}
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
        Dict[str, str]: Basic API information.
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
        Dict[str, Any]: Detailed system health information including:
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
