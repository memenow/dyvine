# Dyvine

A high-performance Python API for interacting with Douyin content.

## Overview

Dyvine provides a high-performance API for downloading and managing Douyin content. It supports various content types including videos, images, live streams and user information.

### Key Features

- **Content Management**: Download videos, images, and live streams
- **Batch Operations**: Efficient bulk content download
- **Performance Optimized**: Asynchronous operations with connection pooling
- **Developer Friendly**: 
  - Type hints throughout
  - Comprehensive documentation
  - Detailed error messages
  - OpenAPI/Swagger documentation

## Prerequisites

- Python 3.13+
- Conda package manager
- Git
- 2GB+ free disk space
- Active internet connection
- Valid Douyin cookie (for authentication)

## Quick Start

1. Set up environment:
```bash
# Clone repository
git clone https://github.com/memenow/dyvine.git
cd dyvine

# Create and activate conda environment
conda env create -f environment.yml
conda activate dyvine

# Install pre-commit hooks
pre-commit install
```

## Configuration

1. Copy the example environment file:
```bash
cp .env.example .env
```

2. Update the `.env` file with your settings:
```env
# API Settings
API_DEBUG=true
API_HOST=0.0.0.0
API_PORT=8000

# Security Settings
SECURITY_SECRET_KEY=your-secret-key
SECURITY_API_KEY=your-api-key

# Douyin Settings
DOUYIN_COOKIE=your-douyin-cookie
DOUYIN_USER_AGENT=your-user-agent
```

## Development

### Running the Server

Start the development server:
```bash
uvicorn src.dyvine.main:app --reload
```

The API will be available at http://localhost:8000

### API Documentation

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

### Code Quality

This project uses several tools to maintain code quality:

- `black` for code formatting
- `isort` for import sorting
- `mypy` for type checking
- `ruff` for linting
- `pytest` for testing
- `bandit` for security checks

Run all checks:
```bash
# Format code
black .
isort .

# Type checking
mypy .

# Linting
ruff check .

# Tests
pytest
```

### Project Structure

```
dyvine/
├── src/dyvine/          # Main package directory
│   ├── core/            # Core functionality (settings, logging)
│   ├── routers/         # API endpoints and request handling
│   ├── schemas/         # Data models and validation
│   └── services/        # Business logic and external integrations
├── .env.example         # Environment variables template
├── environment.yml      # Conda environment specification
├── LICENSE             # Apache 2.0 license
└── README.md           # Project documentation
```

## API Endpoints

Base path: `/api/v1`

### Users

#### Get User Information
```http
GET /api/v1/users/{user_id}
```

#### Download User Content
```http
POST /api/v1/users/{user_id}/content:download
```
Query parameters:
- `include_posts`: Whether to download user's posts (default: true)
- `include_likes`: Whether to download user's liked posts (default: false)
- `max_items`: Maximum number of items to download (optional)

#### Check Operation Status
```http
GET /api/v1/users/operations/{operation_id}
```

### Posts

#### Get Post Details
```http
GET /api/v1/posts/{post_id}
```

#### List User Posts
```http
GET /api/v1/posts/users/{user_id}/posts
```
Query parameters:
- `max_cursor`: Pagination cursor (default: 0)
- `count`: Number of posts per page (default: 20, max: 100)

#### Download User Posts
```http
POST /api/v1/posts/users/{user_id}/posts:download
```
Query parameters:
- `max_cursor`: Starting pagination cursor (default: 0)

### Livestreams

#### Download Active Livestream
```http
POST /api/v1/livestreams/users/{user_id}/stream:download
```
Body parameters:
- `output_path`: Custom save path (optional)

#### Check Download Status
```http
GET /api/v1/livestreams/operations/{operation_id}
```

## Error Handling

The API implements a comprehensive error handling system:

### Exception Types
- `PostNotFoundError`: Post retrieval failures
- `UserNotFoundError`: User lookup failures
- `DownloadError`: Content download issues
- `PostServiceError`: General service errors

### Error Responses
All errors return a structured JSON response containing:
- HTTP status code
- Detailed error message
- Error type classification
- Correlation ID for tracking
- Stack trace (in development mode)

## Monitoring

### Logging
- Structured JSON logging for machine readability
- Request correlation tracking
- Automatic log rotation and archival
- Development/production formatting
- Request/response details
- Performance metrics

### Health Monitoring
The `/health` endpoint provides real-time system metrics:
- Application status and version
- System uptime
- Memory usage and allocation
- CPU utilization
- Disk usage
- Request statistics

Logs and metrics are stored in the `logs/` directory with daily rotation.

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.
