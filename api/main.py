"""Main FastAPI application."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.core.logging import setup_logging
from api.routers import users, posts, lives

# Setup logging
setup_logging()

app = FastAPI(
    title="Dyvine",
    description="API for interacting with Douyin content",
    version="1.0.0"
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(users.router, prefix="/api/v1", tags=["users"])
app.include_router(posts.router, prefix="/api/v1", tags=["posts"])
app.include_router(lives.router, prefix="/api/v1", tags=["lives"])

@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "message": "Welcome to Dyvine API",
        "docs": "/docs",
        "redoc": "/redoc"
    }
