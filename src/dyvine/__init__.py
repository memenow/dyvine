"""Dyvine — FastAPI service for downloading Douyin content.

Subpackages:
    - ``core``: settings, dependency container, operation store, logging,
      path-safety helpers, error handlers.
    - ``services``: domain services (users, posts, livestreams) and the
      R2 storage facade.
    - ``routers``: FastAPI routers, all gated by the ``require_api_key``
      dependency.
    - ``schemas``: Pydantic request/response models.

The runtime entry point is ``dyvine.main:app``.
"""

__version__ = "1.0.0"
