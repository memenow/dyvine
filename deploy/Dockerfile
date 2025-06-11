# Build stage
FROM continuumio/miniconda3:latest AS builder

# Copy environment file first for better caching
COPY environment.yml .

# Create conda environment
RUN conda env create -f environment.yml

# Install additional dependencies
RUN conda run -n dyvine pip install uvicorn gunicorn

# Runtime stage - use smaller base image
FROM continuumio/miniconda3:latest

# Install curl for health check
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Set working directory
WORKDIR /app

# Copy conda environment from builder
COPY --from=builder /opt/conda/envs/dyvine /opt/conda/envs/dyvine

# Create necessary directories with proper permissions
RUN mkdir -p data/douyin/downloads/livestreams \
    && mkdir -p data/douyin/logs/livestreams \
    && mkdir -p logs \
    && chown -R appuser:appuser /app

# Copy application code
COPY src/ ./src/
COPY pyproject.toml .

# Set proper ownership
RUN chown -R appuser:appuser /app

# Set environment variables
ENV PATH=/opt/conda/envs/dyvine/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Start the application with gunicorn
CMD ["gunicorn", "src.dyvine.main:app", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8000", "--timeout", "300", "--keep-alive", "5"]
