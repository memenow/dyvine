"""Batch user-content download CLI (``serial`` / ``concurrent``).

Reads user IDs from a text file (one per line), submits download jobs
to the Dyvine API, polls them to completion, and prints a summary
report. Replaces the former root-level ``batch_download.py`` and
``download_serial.py`` scripts with one tested package:

- environment resolution lives in exactly one place (:mod:`config`),
- the API prefix is configurable (default ``/api/v1``),
- importing this package performs no IO (no ``.env`` reads, no
  network), so unit tests can import it freely.

Usage:
    python -m scripts.dyvine_batch concurrent users.txt --api-key KEY
    python -m scripts.dyvine_batch serial users.txt --api-key KEY
"""

from .cli import main

__all__ = ["main"]
