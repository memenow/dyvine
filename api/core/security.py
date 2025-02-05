"""Security utilities for API authentication and authorization.

This module handles API key validation and security-related functions.
"""

import os
from typing import Optional
from fastapi.security import APIKeyHeader
from api.core.config import settings

# Use API key from settings
API_KEYS = {settings.API_KEY}

def verify_api_key(api_key: Optional[str] = None) -> bool:
    """Verify if the provided API key is valid.
    
    Args:
        api_key: The API key to validate.
        
    Returns:
        bool: True if the API key is valid, False otherwise.
    """
    if not api_key:
        return False
    return api_key in API_KEYS
