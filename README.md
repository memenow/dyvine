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

### Testing

The project uses pytest for testing with a comprehensive test suite covering all major functionality.

#### Test Structure

```
tests/
├── unit/                    # Unit tests
│   ├── core/               # Core functionality tests
│   │   ├── test_logging.py # Logging system tests
│   │   └── test_settings.py# Configuration tests
│   ├── routers/            # API endpoint tests
│   │   ├── test_livestream_router.py
│   │   ├── test_posts_router.py
│   │   └── test_users_router.py
│   ├── schemas/            # Data model tests
│   │   ├── test_livestreams.py
│   │   ├── test_posts.py
│   │   └── test_users.py
│   └── services/           # Business logic tests
│       ├── test_livestream_service.py
│       ├── test_posts_service.py
│       └── test_users_service.py
└── integration/            # Integration tests
    ├── test_api.py        # Full API flow tests
    └── test_douyin.py     # Douyin integration tests
```

#### Running Tests

Run all tests:
```bash
pytest
```

Run specific test categories:
```bash
# Run only unit tests
pytest tests/unit/

# Run specific module tests
pytest tests/unit/core/
pytest tests/unit/routers/
pytest tests/unit/services/

# Run with coverage report
pytest --cov=src/dyvine

# Run with verbose output
pytest -v

# Run specific test file
pytest tests/unit/services/test_livestream_service.py
```

#### Test Features

- **Async Support**: Full support for testing async functions using pytest-asyncio
- **Fixtures**: Comprehensive fixture system for test setup and teardown
- **Mocking**: Extensive use of unittest.mock for external dependencies
- **Parameterization**: Test multiple scenarios efficiently
- **Coverage**: Track test coverage with pytest-cov
- **Error Cases**: Thorough testing of error conditions and edge cases

#### Key Test Areas

1. Core Functionality:
   - Configuration loading and validation
   - Logging system with context tracking
   - Error handling and reporting

2. API Endpoints:
   - Request validation
   - Response formatting
   - Error responses
   - Authentication/authorization

3. Business Logic:
   - Content download operations
   - User data retrieval
   - Stream management
   - Error handling

4. Integration:
   - Full API workflows
   - External service interaction
   - Error recovery and retry logic

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
├── deploy/             # Deployment configurations
│   ├── Dockerfile      # Docker image build configuration
│   └── k8s.yaml        # Kubernetes deployment manifests
├── .env.example         # Environment variables template
├── environment.yml      # Conda environment specification
├── LICENSE             # Apache 2.0 license
└── README.md           # Project documentation
```

## Deployment

### Docker

1. Build the Docker image:
```bash
docker build -t dyvine:latest -f deploy/Dockerfile .
```

2. Run the container:
```bash
docker run -d \
  --name dyvine \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  --env-file .env \
  dyvine:latest
```

### Kubernetes

The application can be deployed to a Kubernetes cluster using the provided configuration in `deploy/k8s.yaml`.

1. Prerequisites:
   - Kubernetes cluster
   - kubectl configured to access your cluster
   - Container registry for storing the Docker image

2. Update configuration:
   - Edit `deploy/k8s.yaml` and update:
     ```yaml
     # In Deployment section
     image: your-registry/dyvine:latest  # Replace with your image repository

     # In Ingress section
     host: your-domain.com  # Replace with your domain
     ```
   - Base64 encode your secrets:
     ```bash
     echo -n "your-secret-key" | base64
     ```
   - Update the secrets in k8s.yaml:
     ```yaml
     # In Secret section
     data:
       SECRET_KEY: <base64-encoded-secret>
       API_KEY: <base64-encoded-api-key>
       DOUYIN_COOKIE: <base64-encoded-cookie>
     ```

3. Deploy to Kubernetes:
```bash
# Apply all resources
kubectl apply -f deploy/k8s.yaml

# Verify deployment
kubectl get pods -l app=dyvine
kubectl get services dyvine
kubectl get ingress dyvine
```

4. Monitor the deployment:
```bash
# Check pod status
kubectl get pods -l app=dyvine

# View logs
kubectl logs -l app=dyvine

# Check service
kubectl get svc dyvine
```

### Production Considerations

1. Security:
   - Use proper secrets management (e.g., HashiCorp Vault)
   - Enable TLS/SSL using cert-manager
   - Set up network policies
   - Configure resource limits

2. Monitoring:
   - Set up Prometheus for metrics
   - Configure logging aggregation
   - Set up alerts for health checks

3. High Availability:
   - Use multiple replicas
   - Configure pod disruption budgets
   - Set up horizontal pod autoscaling
   - Use node affinity and pod anti-affinity

4. Backup:
   - Configure persistent volume backups
   - Set up log archival
   - Implement disaster recovery procedures

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
