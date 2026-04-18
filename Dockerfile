# Build stage - Install dependencies from the locked environment
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY src/ ./src/

RUN pip install --no-cache-dir uv==0.11.6 && \
    uv sync --frozen --no-dev

# Production stage
FROM python:3.12-slim-bookworm AS production

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    VIRTUAL_ENV="/app/.venv"

# Install only runtime dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

WORKDIR /app

# Copy virtual environment and application code
COPY --from=builder /app/.venv /app/.venv
COPY src/ ./src/

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/livez || exit 1

# Run the application
CMD ["/app/.venv/bin/python", "-m", "uvicorn", "src.dyvine.main:app", "--host", "0.0.0.0", "--port", "8000"]
