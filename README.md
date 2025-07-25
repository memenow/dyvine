# 🎵 Dyvine

[![Python Version](https://img.shields.io/badge/python-3.12+-blue.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green.svg)](https://fastapi.tiangolo.com)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Code Style](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Type Hints](https://img.shields.io/badge/typing-mypy-green.svg)](http://mypy-lang.org/)
[![Tests](https://img.shields.io/badge/tests-pytest-orange.svg)](https://pytest.org)

[📖 Documentation](#overview) • [🚀 Quick Start](#quick-start) • [🔧 API Reference](#api-reference) • [🐳 Deployment](#deployment) • [🤝 Contributing](#contributing)

---

**Dyvine** is a production-ready, high-performance REST API designed for content management. It provides comprehensive content download, user management, live streaming, and cloud storage integration capabilities.

**🎯 Core Features** • **⚡ Async Processing** • **🔄 Batch Operations** • **☁️ Cloud Integration** • **📊 Real-time Monitoring** • **🔐 Security**

## Overview

Dyvine provides a comprehensive API for downloading and managing content with production-grade reliability. It supports various content types including videos, images, live streams, and user information with built-in cloud storage integration.

### Key Features

- **📥 Content Management**: Download videos, images, and live streams
- **👥 User Operations**: Retrieve user profiles and content analytics
- **⚡ Batch Processing**: Efficient bulk content download operations
- **🏗️ Architecture**: Async operations with connection pooling
- **☁️ Cloud Storage**: Direct integration with object storage
- **🔧 Developer Experience**:
  - Complete type hints throughout codebase
  - Detailed error messages and logging
  - Auto-generated OpenAPI/Swagger documentation
  - Production-ready configuration management

## Quick Start

### Prerequisites

- Python 3.12+
- Git
- 2GB+ free disk space
- Active internet connection
- Valid authentication cookie
- Optional: Object storage credentials

### Installation

```bash
# Clone repository
git clone https://github.com/memenow/dyvine.git
cd dyvine

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -e .

# Install development dependencies (optional)
pip install -e ".[dev]"
```

### Configuration

1. **Environment Setup**:

   ```bash
   cp .env.example .env
   ```

2. **Required Configuration**:

   Edit `.env` file with your settings:

   ```bash
   # Essential settings
   DOUYIN_COOKIE=your_cookie_here
   
   # Security (change in production)
   SECURITY_SECRET_KEY=your-production-secret-key
   SECURITY_API_KEY=your-production-api-key
   
   # Optional: Object storage integration
   R2_ACCOUNT_ID=your_account_id
   R2_ACCESS_KEY_ID=your_access_key
   R2_SECRET_ACCESS_KEY=your_secret_key
   R2_BUCKET_NAME=your_bucket_name
   ```

### Running the Application

```bash
# Start development server
uvicorn src.dyvine.main:app --reload

# Production server
uvicorn src.dyvine.main:app --host 0.0.0.0 --port 8000
```

The API will be available at:

- **Application**: <http://localhost:8000>
- **Interactive Documentation**: <http://localhost:8000/docs>
- **Alternative Documentation**: <http://localhost:8000/redoc>

## API Reference

### Base URL

```text
http://localhost:8000/api/v1
```

### Core Endpoints

#### User Operations

```http
# Get user information
GET /api/v1/users/{user_id}

# Download user content
POST /api/v1/users/{user_id}/content:download
```

#### Post Management

```http
# Get post details
GET /api/v1/posts/{post_id}

# List user posts
GET /api/v1/posts/users/{user_id}/posts

# Download user posts
POST /api/v1/posts/users/{user_id}/posts:download
```

#### Live Streaming

```http
# Download active livestream
POST /api/v1/livestreams/users/{user_id}/stream:download

# Download from URL
POST /api/v1/livestreams/stream:download

# Check download status
GET /api/v1/livestreams/operations/{operation_id}
```

### Example Usage

**Download User Posts**:

```bash
curl -X POST "http://localhost:8000/api/v1/posts/users/USER_ID/posts:download" \
     -H "Content-Type: application/json"
```

**Get User Information**:

```bash
curl "http://localhost:8000/api/v1/users/USER_ID"
```

## Testing

The project includes a comprehensive test suite with full async support:

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=src/dyvine

# Run specific test categories
pytest tests/unit/          # Unit tests
pytest tests/integration/   # Integration tests

# Run with verbose output
pytest -v
```

### Test Structure

```text
tests/
├── unit/                 # Unit tests
│   ├── core/            # Core functionality
│   ├── routers/         # API endpoints
│   ├── schemas/         # Data models
│   └── services/        # Business logic
└── integration/         # Integration tests
    ├── test_api.py      # Full API workflows
    └── test_douyin.py   # External service integration
```

## Deployment

### Docker Deployment

1. **Build Image**:

   ```bash
   docker build -t dyvine:latest -f deploy/Dockerfile .
   ```

2. **Run Container**:

   ```bash
   docker run -d \
     --name dyvine \
     -p 8000:8000 \
     -v $(pwd)/data:/app/data \
     -v $(pwd)/logs:/app/logs \
     --env-file .env \
     dyvine:latest
   ```

### Kubernetes Deployment

1. **Prerequisites**:
   - Kubernetes cluster
   - kubectl configured
   - Container registry access

2. **Deploy**:

   ```bash
   # Update image reference in deploy/k8s.yaml
   kubectl apply -f deploy/k8s.yaml
   
   # Verify deployment
   kubectl get pods -l app=dyvine
   kubectl get services dyvine
   ```

### Production Considerations

- **Security**: Use proper secrets management (HashiCorp Vault)
- **Monitoring**: Set up Prometheus metrics and logging aggregation
- **High Availability**: Configure multiple replicas and autoscaling
- **Backup**: Implement persistent volume and log archival strategies

## Monitoring and Logging

### Health Monitoring

```http
GET /health
```

Response includes:

- Application status and version
- System uptime and resource usage
- Request statistics
- Memory and CPU metrics

### Logging Features

- Structured JSON logging for machine readability
- Request correlation tracking
- Automatic log rotation and archival
- Development/production formatting modes
- Performance metrics collection

### Development Commands

```bash
# Code formatting
black .
isort .

# Type checking
mypy .

# Linting
ruff check .

# Run all checks
pytest && black . && isort . && mypy . && ruff check .
```

### CI/CD Pipeline

The project uses GitHub Actions for continuous integration and deployment:

- **Code Quality**: Automated linting, formatting, and type checking
- **Testing**: Comprehensive test suite with coverage reporting
- **Security**: Vulnerability scanning with Trivy, Safety, and Bandit
- **Docker**: Multi-platform image builds and pushes to GitHub Container Registry
- **Releases**: Automated release creation for version tags

#### Available Workflows

- `ci-cd.yml`: Main CI/CD pipeline (runs on push/PR)
- `code-quality.yml`: Code quality checks (runs on PR)
- `dependency-check.yml`: Weekly security dependency audit

#### Docker Images

Images are automatically built and pushed to:

```text
ghcr.io/memenow/prod/dyvine:latest
ghcr.io/memenow/prod/dyvine:<version>
ghcr.io/memenow/prod/dyvine:<branch>-<sha>
```

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.
