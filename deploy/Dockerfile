# Build stage
FROM continuumio/miniconda3:latest AS builder

# Copy environment file
COPY environment.yml .

# Create conda environment
RUN conda env create -f environment.yml

# Install additional dependencies
RUN conda run -n dyvine pip install uvicorn gunicorn

# Runtime stage
FROM continuumio/miniconda3:latest

# Set working directory
WORKDIR /app

# Copy conda environment from builder
COPY --from=builder /opt/conda/envs/dyvine /opt/conda/envs/dyvine

# Copy application code
COPY src/ ./src/
COPY pyproject.toml .

# Create necessary directories
RUN mkdir -p data/douyin/downloads/livestreams \
    && mkdir -p data/douyin/logs/livestreams \
    && mkdir -p logs

# Set environment variables
ENV PATH /opt/conda/envs/dyvine/bin:$PATH
ENV PYTHONPATH /app

# Expose port
EXPOSE 8000

# Start the application with gunicorn
CMD ["gunicorn", "src.dyvine.main:app", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8000"]
