"""Deployment contract: a single canonical import path (``dyvine.*``).

The package lives under ``src/dyvine`` and tests resolve it as the
top-level ``dyvine`` package. Production must use the same spelling:
``src.dyvine.main:app`` (namespace-package import through the repo
root) would register every module-level Prometheus metric a second
time if both spellings ever load in one process.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_production_entrypoint_uses_canonical_import_path() -> None:
    """Docker CMD, Makefile and README must agree on ``dyvine.main:app``."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    for name, content in (
        ("Dockerfile", dockerfile),
        ("Makefile", makefile),
        ("README.md", readme),
    ):
        assert (
            "src.dyvine.main:app" not in content
        ), f"{name} still references the legacy src.dyvine import path"
        assert (
            "dyvine.main:app" in content
        ), f"{name} must reference the canonical dyvine.main:app entry point"
