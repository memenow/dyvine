"""Regression: distribution metadata contracts pinned in one place.

Covers the pyproject review findings that need a standing guard instead of
a one-time edit: version-line consistency, the bare-module entry-point
convention, the httpx dual-surface bounds, and the wheel's
directory-only artifacts.
"""

from __future__ import annotations

import ast
import importlib
import tomllib
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())


def _plugin_manifest() -> dict:
    return yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text())


def _dunder_version(package: str) -> str:
    """Read ``__version__`` from a src package without importing it.

    ``import dyvine`` under pytest resolves to the repo-root directory-plugin
    shim (which has no ``__version__``), so parse the assignment instead.
    """
    init = REPO_ROOT / "src" / package / "__init__.py"
    for node in ast.parse(init.read_text()).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__version__"
            and isinstance(node.value, ast.Constant)
        ):
            return str(node.value.value)
    raise AssertionError(f"no __version__ assignment in {init}")


def test_distribution_version_matches_engine_package() -> None:
    """The ``dyvine`` distribution and its engine package share one version."""
    dist_version = _pyproject()["project"]["version"]
    assert dist_version == _dunder_version("dyvine")
    assert dist_version == "1.0.0"


def test_plugin_manifest_version_matches_plugin_package() -> None:
    """Directory installs (``plugin.yaml``) and the plugin package agree."""
    manifest_version = _plugin_manifest()["version"]
    assert manifest_version == _dunder_version("dyvine_hermes")
    assert manifest_version == "1.0.0"


def test_all_version_sites_agree() -> None:
    """One version line: dist, manifest, and both packages match."""
    dist_version = _pyproject()["project"]["version"]
    assert _plugin_manifest()["version"] == dist_version
    assert _dunder_version("dyvine") == dist_version
    assert _dunder_version("dyvine_hermes") == dist_version


def test_entry_point_is_bare_module_with_register() -> None:
    """The entry point stays a bare module: hermes loads it, then calls
    ``register`` (see the comment above the entry point in pyproject)."""
    entry_points = _pyproject()["project"]["entry-points"]["hermes_agent.plugins"]
    target = entry_points["dyvine"]
    assert ":" not in target, f"entry point must stay a bare module, got {target!r}"
    module = importlib.import_module(target)
    assert str(REPO_ROOT / "src") in str(module.__file__)
    assert callable(module.register)


def _httpx_requirement() -> Requirement:
    for dep in _pyproject()["project"]["dependencies"]:
        req = Requirement(dep)
        if req.name == "httpx":
            return req
    raise AssertionError("httpx not found in project dependencies")


def test_httpx_floor_admits_f2_pin() -> None:
    """The floor must keep admitting f2 0.0.1.7's ``httpx==0.27.2`` pin or
    pip installs become unresolvable (f2 pins with ``==``)."""
    req = _httpx_requirement()
    assert req.specifier.contains("0.27.2", prereleases=True), (
        f"httpx floor {req.specifier} no longer admits f2's ==0.27.2 pin"
    )


def test_httpx_override_floor_stays_modern() -> None:
    """The uv override keeps the lockfile on the 0.28+ surface."""
    overrides = _pyproject()["tool"]["uv"]["override-dependencies"]
    for entry in overrides:
        req = Requirement(entry)
        if req.name != "httpx":
            continue
        floors = [
            Version(spec.version) for spec in req.specifier if spec.operator == ">="
        ]
        assert floors and max(floors) >= Version("0.28.1"), (
            f"httpx override {req.specifier} dropped below 0.28.1"
        )
        return
    raise AssertionError("httpx not found in override-dependencies")


#: httpx APIs dyvine uses, each present in both 0.27.2 and 0.28.1. The only
#: 0.27 -> 0.28 removal is the deprecated ``app``/``proxies`` Client kwargs
#: (verified by signature diff); extend this set only after confirming the
#: new API exists on BOTH surfaces.
_DUAL_SURFACE_HTTPX_ATTRS = frozenset(
    {"AsyncClient", "Client", "HTTPError", "HTTPStatusError"}
)

#: Client kwargs removed in httpx 0.28; must never appear in dyvine's own
#: httpx usage (f2's usage is upstream's responsibility).
_REMOVED_HTTPX_KWARGS = frozenset({"app", "proxies"})


def _iter_src_trees() -> list[tuple[Path, ast.Module]]:
    trees = []
    for package in ("src/dyvine", "src/dyvine_hermes"):
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            trees.append((path, ast.parse(path.read_text())))
    return trees


def test_dyvine_httpx_usage_stays_dual_surface() -> None:
    """Dyvine's own httpx usage must work on both the pip floor (0.27.2)
    and the locked override (0.28+)."""
    violations: list[str] = []
    for path, tree in _iter_src_trees():
        httpx_aliases = {"httpx"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "httpx":
                        httpx_aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module == "httpx":
                for alias in node.names:
                    if alias.name not in _DUAL_SURFACE_HTTPX_ATTRS:
                        violations.append(
                            f"{path.relative_to(REPO_ROOT)}: "
                            f"new httpx import {alias.name!r} — verify it "
                            "exists in 0.27.2 before merging"
                        )
            elif isinstance(node, ast.Attribute):
                if (
                    isinstance(node.value, ast.Name)
                    and node.value.id in httpx_aliases
                    and node.attr not in _DUAL_SURFACE_HTTPX_ATTRS
                ):
                    violations.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                        f"new httpx API httpx.{node.attr} — verify it "
                        "exists in 0.27.2 before merging"
                    )
            elif isinstance(node, ast.Call):
                func = node.func
                is_httpx_client = (
                    isinstance(func, ast.Attribute)
                    and func.attr in {"Client", "AsyncClient"}
                    and isinstance(func.value, ast.Name)
                    and func.value.id in httpx_aliases
                )
                if not is_httpx_client:
                    continue
                for keyword in node.keywords:
                    if keyword.arg in _REMOVED_HTTPX_KWARGS:
                        violations.append(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                            f"httpx Client kwarg {keyword.arg!r} was removed "
                            "in 0.28"
                        )
    assert not violations, "\n".join(violations)


def test_wheel_ships_only_src_packages() -> None:
    """plugin.yaml and alembic/ stay directory-distribution only: the wheel
    ships exactly the two src packages, with no force-includes."""
    wheel = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/dyvine", "src/dyvine_hermes"]
    assert "force-include" not in wheel and "force_include" not in wheel


def test_manifest_dependencies_mirror_pyproject() -> None:
    """plugin.yaml python_dependencies stay in sync with pyproject bounds
    (except the operator-side alembic, which is directory-only)."""
    pyproject_deps = {
        Requirement(dep).name: Requirement(dep).specifier
        for dep in _pyproject()["project"]["dependencies"]
    }
    manifest_deps = {
        Requirement(dep).name: Requirement(dep).specifier
        for dep in _plugin_manifest()["python_dependencies"]
    }
    assert set(manifest_deps) == set(pyproject_deps) - {"alembic"}, (
        f"manifest deps {sorted(manifest_deps)} drifted from "
        f"pyproject deps {sorted(pyproject_deps)}"
    )

    def _normalized(spec: SpecifierSet) -> set[tuple[str, Version]]:
        return {(item.operator, Version(item.version)) for item in spec}

    for name, spec in manifest_deps.items():
        assert _normalized(SpecifierSet(str(spec))) == _normalized(
            pyproject_deps[name]
        ), f"{name}: manifest {spec} != pyproject {pyproject_deps[name]}"
