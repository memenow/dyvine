# Dyvine

A FastAPI-based service for interacting with Douyin content, enabling post downloads and user data retrieval.

## Features

- User Profile Retrieval
- Post Details & Download
- Bulk Post Downloads (Videos & Images)
- Live Stream Information
- API Key Authentication
- Rate Limiting
- CORS Support
- Structured Logging

## Prerequisites

- Conda package manager
- Python 3.13.1
- Git

## Installation

1. Clone the repository:
```bash
git clone https://github.com/memenow/dyvine.git
cd dyvine
```

2. Create and activate conda environment:
```bash
conda env create -f environment.yml
conda activate dyvine
```

3. Create `.env` file:
```env
API_KEY=your-api-key
DOUYIN_COOKIE=your-douyin-cookie
DOUYIN_USER_AGENT=your-user-agent
DEBUG=False
```

## API Endpoints

### Users
- `GET /api/v1/users/{sec_user_id}/profile` - Get user profile
- `GET /api/v1/users/{sec_user_id}/following` - Get user following list

### Posts
- `GET /api/v1/posts/{aweme_id}` - Get post details
- `GET /api/v1/users/{sec_user_id}/posts` - Get user posts
- `POST /api/v1/users/{sec_user_id}/download-all` - Download all user posts

### Lives
- `GET /api/v1/lives/{webcast_id}` - Get live stream information

## Authentication

All endpoints require an API key passed via the `X-API-Key` header:

```bash
curl -H "X-API-Key: your-api-key" http://localhost:8000/api/v1/users/123/profile
```

## Configuration

Key settings in `config.py`:
- API authentication
- CORS settings
- Rate limiting
- Douyin-specific configurations
- Proxy settings
- Download parameters

## Running the Server

Development:
```bash
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

Production:
```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

## API Documentation

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## Error Handling

The API uses standard HTTP status codes:
- 200: Success
- 400: Bad Request
- 401: Unauthorized
- 404: Not Found
- 500: Internal Server Error

## Development Setup

### Using Conda

You can update the environment when dependencies change:
```bash
conda env update -f environment.yml
```

To remove the environment:
```bash
conda deactivate
conda env remove -n dyvine
```

To export your current environment:
```bash
conda env export > environment.yml
```

### Environment Variables

Required environment variables in `.env`:
```env
API_KEY=your-api-key
DOUYIN_COOKIE=your-douyin-cookie
DOUYIN_USER_AGENT=your-user-agent
DEBUG=False
```

## License

This project is licensed under the Apache 2.0 License. For more details, please refer to the [ LICENSE ](LICENSE) file.
