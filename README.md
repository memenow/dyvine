# Dyvine

Dyvine is a high-performance Python API for interacting with Douyin (TikTok) content, providing functionalities to download videos, images, live streams, and retrieve user information. It now also includes integration with Cloudflare R2 for efficient storage of downloaded content.

**GitHub Repository:** [https://github.com/memenow/dyvine](https://github.com/memenow/dyvine)

## Overview

Dyvine provides a high-performance API for downloading and managing Douyin content. It supports various content types including videos, images, live streams and user information.

### Table of Contents

1.  [Overview](#overview)
    -   [Key Features](#key-features)
2.  [Prerequisites](#prerequisites)
3.  [Quick Start](#quick-start)
4.  [Configuration](#configuration)
5.  [Cloudflare R2 Integration](#cloudflare-r2-integration)
6.  [Development](#development)
    -   [Running the Server](#running-the-server)
    -   [API Documentation](#api-documentation)
    -   [Testing](#testing)
        -   [Test Structure](#test-structure)
        -   [Running Tests](#running-tests)
        -   [Test Features](#test-features)
        -   [Key Test Areas](#key-test-areas)
    -   [Code Quality](#code-quality)
    -   [Project Structure](#project-structure)
7.  [Deployment](#deployment)
    -   [Docker](#docker)
    -   [Kubernetes](#kubernetes)
    -   [Production Considerations](#production-considerations)
8.  [API Endpoints](#api-endpoints)
    -   [Users](#users)
    -   [Posts](#posts)
    -   [Livestreams](#livestreams)
9.  [Error Handling](#error-handling)
    -   [Exception Types](#exception-types)
    -   [Error Responses](#error-responses)
10. [Monitoring](#monitoring)
    -   [Logging](#logging)
    -   [Health Monitoring](#health-monitoring)
11. [Contributing](#contributing)
12. [License](#license)

### Key Features

*   **Content Management:** Download videos, images, and live streams.
*   **Batch Operations:** Efficient bulk content download.
*   **Performance Optimized:** Asynchronous operations with connection pooling.
*   **Cloudflare R2 Integration:** Store downloaded content directly to Cloudflare R2.
*   **Developer Friendly:**
    *   Type hints throughout
    *   Comprehensive documentation
    *   Detailed error messages
    *   OpenAPI/Swagger documentation

## Prerequisites

*   Python 3.13+
*   Conda package manager (optional, but recommended)
*   Git
*   2GB+ free disk space
*   Active internet connection
*   Valid Douyin cookie (for authentication)
*   Cloudflare account and R2 credentials (if using R2 storage)

## Quick Start

1.  Set up environment:

    ```bash
    # Clone repository
    git clone https://github.com/memenow/dyvine.git
    cd dyvine

    # Create and activate conda environment (optional, but recommended)
    conda env create -f environment.yml
    conda activate dyvine

    # Install dependencies using pip (managed by pyproject.toml)
    pip install .

    # Install pre-commit hooks
    pre-commit install
    ```

## Configuration

1.  Copy the example environment file:

    ```bash
    cp .env.example .env
    ```

2.  Update the `.env` file with your settings:

    ```env
    # API Settings
    API_DEBUG=true
    API_HOST=0.0.0.0
    API_PORT=8000

    # Security Settings
    SECURITY_SECRET_KEY=your-secret-key
    SECURITY_API_KEY=your-api-key

    # Cloudflare R2 Settings (optional)
    R2_ACCOUNT_ID=your-cloudflare-account-id
    R2_ACCESS_KEY_ID=your-r2-access-key-id
    R2_SECRET_ACCESS_KEY=your-r2-secret-access-key
    R2_BUCKET_NAME=your-r2-bucket-name
    R2_ENDPOINT=https://<account_id>.r2.cloudflarestorage.com

    # Douyin Settings
    DOUYIN_COOKIE=your-douyin-cookie
    DOUYIN_USER_AGENT=your-user-agent
    ```

## Cloudflare R2 Integration

Dyvine includes integration with Cloudflare R2 for efficient and scalable storage of downloaded content. To enable R2 integration:

1.  **Create a Cloudflare account and R2 bucket:** If you don't already have one, create a Cloudflare account and an R2 bucket.
2.  **Generate R2 API credentials:** Create an API token with "Edit" access to your R2 bucket.
3.  **Configure environment variables:** Set the following environment variables in your `.env` file:
    *   `R2_ACCOUNT_ID`: Your Cloudflare account ID.
    *   `R2_ACCESS_KEY_ID`: Your R2 access key ID.
    *   `R2_SECRET_ACCESS_KEY`: Your R2 secret access key.
    *   `R2_BUCKET_NAME`: The name of your R2 bucket.
    *   `R2_ENDPOINT`: The endpoint URL for your R2 bucket (e.g., `https://<account_id>.r2.cloudflarestorage.com`).

With these settings configured, Dyvine will automatically upload downloaded content to your R2 bucket. The storage paths follow a specific structure:

*   **User-generated content (images/videos):** `content_type/user_id/date_prefix_safe_filename_uuid8.ext`
*   **Livestream recordings:** `livestreams/user_id/stream_id/recording_timestamp.mp4`

## Development

### Running the Server

Start the development server:

```bash
uvicorn src.dyvine.main:app --reload
```

The API will be available at http://localhost:8000

### API Documentation

*   Swagger UI: http://localhost:8000/docs
*   ReDoc: http://localhost:8000/redoc

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
│       ├── test_storage.py # Added storage service tests
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

*   **Async Support:** Full support for testing async functions using pytest-asyncio.
*   **Fixtures:** Comprehensive fixture system for test setup and teardown.
*   **Mocking:** Extensive use of unittest.mock and `boto3`'s stubber for external dependencies.
*   **Parameterization:** Test multiple scenarios efficiently.
*   **Coverage:** Track test coverage with pytest-cov.
*   **Error Cases:** Thorough testing of error conditions and edge cases.

#### Key Test Areas

1.  Core Functionality:
    *   Configuration loading and validation.
    *   Logging system with context tracking.
    *   Error handling and reporting.

2.  API Endpoints:
    *   Request validation.
    *   Response formatting.
    *   Error responses.
    *   Authentication/authorization.

3.  Business Logic:
    *   Content download operations.
    *   User data retrieval.
    *   Stream management.
    *   Cloudflare R2 integration (upload, metadata, deletion).
    *   Error handling.

4.  Integration:
    *   Full API workflows.
    *   External service interaction (Douyin, Cloudflare R2).
    *   Error recovery and retry logic.

### Code Quality

This project uses several tools to maintain code quality:

*   `black` for code formatting
*   `isort` for import sorting
*   `mypy` for type checking
*   `ruff` for linting
*   `pytest` for testing
*   `bandit` for security checks

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
├── environment.yml      # Conda environment specification (for environment creation)
├── pyproject.toml       # Project metadata and dependencies
├── LICENSE             # Apache 2.0 license
└── README.md           # Project documentation
```

## Deployment

### Docker

1.  Build the Docker image:

    ```bash
    docker build -t dyvine:latest -f deploy/Dockerfile .
    ```

2.  Run the container:

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

1.  Prerequisites:
    *   Kubernetes cluster
    *   kubectl configured to access your cluster
    *   Container registry for storing the Docker image

2.  Update configuration:
    *   Edit `deploy/k8s.yaml` and update:

        ```yaml
        # In Deployment section
        image: your-registry/dyvine:latest  # Replace with your image repository

        # In Ingress section
        host: your-domain.com  # Replace with your domain
        ```

    *   Base64 encode your secrets:

        ```bash
        echo -n "your-secret-key" | base64
        ```

    *   Update the secrets in k8s.yaml:

        ```yaml
        # In Secret section
        data:
          SECRET_KEY: <base64-encoded-secret>
          API_KEY: <base64-encoded-api-key>
          DOUYIN_COOKIE: <base64-encoded-cookie>
          R2_ACCOUNT_ID: <base64-encoded-account-id>
          R2_ACCESS_KEY_ID: <base64-encoded-access-key-id>
          R2_SECRET_ACCESS_KEY: <base64-encoded-secret-access-key>
          R2_BUCKET_NAME: <base64-encoded-bucket-name>
        ```

3.  Deploy to Kubernetes:

    ```bash
    # Apply all resources
    kubectl apply -f deploy/k8s.yaml

    # Verify deployment
    kubectl get pods -l app=dyvine
    kubectl get services dyvine
    kubectl get ingress dyvine
    ```

4.  Monitor the deployment:

    ```bash
    # Check pod status
    kubectl get pods -l app=dyvine

    # View logs
    kubectl logs -l app=dyvine

    # Check service
    kubectl get svc dyvine
    ```

### Production Considerations

1.  Security:
    *   Use proper secrets management (e.g., HashiCorp Vault).
    *   Enable TLS/SSL using cert-manager.
    *   Set up network policies.
    *   Configure resource limits.

2.  Monitoring:
    *   Set up Prometheus for metrics.
    *   Configure logging aggregation.
    *   Set up alerts for health checks.

3.  High Availability:
    *   Use multiple replicas.
    *   Configure pod disruption budgets.
    *   Set up horizontal pod autoscaling.
    *   Use node affinity and pod anti-affinity.

4.  Backup:
    *   Configure persistent volume backups.
    *   Set up log archival.
    *   Implement disaster recovery procedures.

## API Endpoints

Base path: `/api/v1`

### Users

#### Get User Information

```http
GET /api/v1/users/{user_id}
```

**Example:**

```http
GET /api/v1/users/MS4wLjABAAAA-kxe2_w-i_5F_q_b_rX_vIDqfwyTNYvM-oDD_eRjQVc
```

**Response:**

```json
{
  "user_id": "MS4wLjABAAAA-kxe2_w-i_5F_q_b_rX_vIDqfwyTNYvM-oDD_eRjQVc",
  "nickname": "Example User",
  "avatar_url": "https://example.com/avatar.jpg",
  "signature": "Example signature",
  "following_count": 100,
  "follower_count": 200,
  "total_favorited": 300,
  "is_living": false,
  "room_id": null
}
```

#### Download User Content

```http
POST /api/v1/users/{user_id}/content:download
```

**Example:**

```http
POST /api/v1/users/MS4wLjABAAAA-kxe2_w-i_5F_q_b_rX_vIDqfwyTNYvM-oDD_eRjQVc/content:download?include_posts=true&include_likes=false&max_items=10
```

**Response:**

```json
{
  "task_id": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
  "status": "pending",
  "message": "Download started",
  "progress": 0
}
```

Query parameters:

*   `include_posts`: Whether to download user's posts (default: true).
*   `include_likes`: Whether to download user's liked posts (default: false).
*   `max_items`: Maximum number of items to download (optional).

#### Check Operation Status

```http
GET /api/v1/users/operations/{operation_id}
```

**Example:**

```http
GET /api/v1/users/operations/a1b2c3d4-e5f6-7890-1234-567890abcdef
```

**Response:**

```json
{
  "task_id": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
  "status": "completed",
  "message": "Download completed",
  "progress": 100,
  "total_items": 10,
  "downloaded_items": 10,
  "error": null
}
```

### Posts

#### Get Post Details

```http
GET /api/v1/posts/{post_id}
```

**Example:**

```http
GET /api/v1/posts/7123456789012345678
```

**Response:**

```json
{
  "aweme_id": "7123456789012345678",
  "desc": "Example post description",
  "create_time": 1678886400,
  "post_type": "video",
  "video_info": {
    "play_addr": "https://example.com/video.mp4",
    "duration": 60,
    "ratio": "16:9",
    "width": 1920,
    "height": 1080
  },
  "images": null,
  "statistics": {
    "digg_count": 1000,
    "comment_count": 200,
    "share_count": 50
  }
}
```

#### List User Posts

```http
GET /api/v1/posts/users/{user_id}/posts
```

**Example:**

```http
GET /api/v1/posts/users/MS4wLjABAAAA-kxe2_w-i_5F_q_b_rX_vIDqfwyTNYvM-oDD_eRjQVc/posts?max_cursor=0&count=20
```

**Response:**

```json
[
  {
    "aweme_id": "7123456789012345678",
    "desc": "Example post description 1",
    "create_time": 1678886400,
    "post_type": "video",
    "video_info": {
      "play_addr": "https://example.com/video1.mp4",
      "duration": 60,
      "ratio": "16:9",
      "width": 1920,
      "height": 1080
    },
    "images": null,
    "statistics": {
      "digg_count": 1000,
      "comment_count": 200,
      "share_count": 50
    }
  },
  {
    "aweme_id": "7123456789012345679",
    "desc": "Example post description 2",
    "create_time": 1678886500,
    "post_type": "images",
    "video_info": null,
    "images": [
      {
        "url": "https://example.com/image1.jpg",
        "width": 1080,
        "height": 1920
      }
    ],
    "statistics": {
      "digg_count": 500,
      "comment_count": 100,
      "share_count": 25
    }
  }
]
```

Query parameters:

*   `max_cursor`: Pagination cursor (default: 0).
*   `count`: Number of posts per page (default: 20, max: 100).

#### Download User Posts

```http
POST /api/v1/posts/users/{user_id}/posts:download
```

**Example:**

```http
POST /api/v1/posts/users/MS4wLjABAAAA-kxe2_w-i_5F_q_b_rX_vIDqfwyTNYvM-oDD_eRjQVc/posts:download?max_cursor=0
```

**Response:**

```json
{
  "sec_user_id": "MS4wLjABAAAA-kxe2_w-i_5F_q_b_rX_vIDqfwyTNYvM-oDD_eRjQVc",
  "download_path": "/path/to/downloads/Example User",
  "total_posts": 100,
  "downloaded_count": {
    "video": 50,
    "images": 25,
    "mixed": 10,
    "live": 5,
    "collection": 5,
    "story": 5,
    "unknown": 0
  },
  "total_downloaded": 95,
  "status": "partial_success",
  "message": "Downloaded 95 out of 100 posts. (Videos: 50, Images: 25, Mixed: 10, Lives: 5, Collections: 5, Stories: 0) Files saved to /path/to/downloads/Example User",
  "error_details": null
}
```

Query parameters:

*   `max_cursor`: Starting pagination cursor (default: 0).

### Livestreams

#### Download Active Livestream

```http
POST /api/v1/livestreams/users/{user_id}/stream:download
```

**Example:**

```http
POST /api/v1/livestreams/users/MS4wLjABAAAA-kxe2_w-i_5F_q_b_rX_vIDqfwyTNYvM-oDD_eRjQVc/stream:download
```

**Response:**

```json
{
  "status": "pending",
  "download_path": "/path/to/downloads/livestreams/MS4wLjABAAAA-kxe2_w-i_5F_q_b_rX_vIDqfwyTNYvM-oDD_eRjQVc",
  "error": null
}
```

Body parameters:

*   `output_path`: Custom save path (optional).

#### Check Download Status

```http
GET /api/v1/livestreams/operations/{operation_id}
```

**Example:**

```http
GET /api/v1/livestreams/operations/a1b2c3d4-e5f6-7890-1234-567890abcdef
```

**Response:**

```json
{
  "status": "success",
  "download_path": "/path/to/downloads/livestreams/MS4wLjABAAAA-kxe2_w-i_5F_q_b_rX_vIDqfwyTNYvM-oDD_eRjQVc_merged.mp4",
  "error": null
}
```

## Error Handling

The API implements a comprehensive error handling system:

### Exception Types

*   `PostNotFoundError`: Post retrieval failures.
*   `UserNotFoundError`: User lookup failures.
*   `DownloadError`: Content download issues.
*   `LivestreamError`: Livestream-related errors.
*   `PostServiceError`: General service errors.
*   `UserServiceError`: General user service errors.

### Error Responses

All errors return a structured JSON response containing:

*   HTTP status code
*   Detailed error message
*   Error type classification
*   Correlation ID for tracking
*   Stack trace (in development mode)

## Monitoring

### Logging

*   Structured JSON logging for machine readability.
*   Request correlation tracking.
*   Automatic log rotation and archival.
*   Development/production formatting.
*   Request/response details.
*   Performance metrics.

### Health Monitoring

The `/health` endpoint provides real-time system metrics:

*   Application status and version
*   System uptime
*   Memory usage and allocation
*   CPU utilization
*   Disk usage
*   Request statistics

Logs and metrics are stored in the `logs/` directory with daily rotation.

## Contributing

Contributions to Dyvine are welcome! Please follow these guidelines:

1.  **Fork the repository:** Create a fork of the Dyvine repository on GitHub.
2.  **Create a branch:** Create a new branch for your feature or bug fix. Use a descriptive name, such as `feature/add-new-endpoint` or `fix/resolve-issue-123`.
3.  **Make your changes:** Implement your changes, ensuring that you follow the project's coding style and conventions.
4.  **Write tests:** Add unit tests and/or integration tests to cover your changes. Ensure that all tests pass before submitting a pull request.
5.  **Run checks:** Run all code quality checks (formatting, linting, type checking) to ensure consistency.
6.  **Commit your changes:** Commit your changes with clear and concise commit messages.
7.  **Push to your fork:** Push your changes to your forked repository.
8.  **Submit a pull request:** Create a pull request from your branch to the `main` branch of the Dyvine repository. Provide a detailed description of your changes and reference any related issues.
9.  **Code Review:** The project maintainers will review your pull request and may request changes or clarifications. Be responsive to feedback and make necessary updates.
10. **Merge:** Once your pull request is approved, it will be merged into the `main` branch.

Please report any bugs or issues on the [GitHub Issues](https://github.com/memenow/dyvine/issues) page.

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.
