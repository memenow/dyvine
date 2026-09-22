"""Dyvine — Douyin download engine behind the hermes plugin.

Subpackages:
    - ``core``: settings, background tasks, logging, path-safety
      helpers, pagination.
    - ``db``: Postgres repositories (operations, queue, seeds, send
      status, profiles, rounds, watch) plus the session factory.
    - ``services``: domain services (users, posts, livestreams,
      queue, profiles, delivery) and the R2 storage facade.
    - ``schemas``: Pydantic models shared by the services.

There is no HTTP surface: the hermes plugin in
``dyvine_hermes`` (see ``plugin.yaml``) is the only interface.
"""

__version__ = "1.0.0"
