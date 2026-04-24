# Build stage - Install dependencies from the locked environment
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app

# Apply pending security patches from the base distribution before any
# further tooling lands on top. This is what keeps the Trivy gate green
# on releases of python:3.12-slim-bookworm that lag behind Debian's
# latest security advisories.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

COPY pyproject.toml uv.lock ./
COPY src/ ./src/

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir uv==0.11.6 && \
    uv sync --frozen --no-dev && \
    # ``f2`` (the upstream Douyin SDK) lists ``black`` as a runtime
    # dependency even though it never imports the formatter. ``uv sync
    # --no-dev`` therefore still pulls the Black wheel, and the binary
    # ends up in the production image with historical CVEs that Trivy
    # flags. Purge the dist-info, the packaged library, and both CLI
    # entry points so nothing of Black ships downstream. Keep this in
    # the Dockerfile (not in ``pyproject.toml`` as an override) so the
    # developer venv can still rely on black for local formatting.
    rm -rf /app/.venv/bin/black /app/.venv/bin/blackd \
        /app/.venv/lib/python*/site-packages/black \
        /app/.venv/lib/python*/site-packages/blackd \
        /app/.venv/lib/python*/site-packages/black-*.dist-info \
        /app/.venv/lib/python*/site-packages/blackd-*.dist-info

# Production stage
FROM python:3.12-slim-bookworm AS production

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    VIRTUAL_ENV="/app/.venv"

# Apply pending security patches in the runtime stage too, then install
# only the runtime packages we actually need.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends curl \
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
