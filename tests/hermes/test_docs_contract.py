"""Static docs stay pinned to the manifest instead of drifting silently."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _manifest_tools() -> list[str]:
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    tools = manifest["provides_tools"]
    assert isinstance(tools, list) and tools
    return list(tools)


def test_docs_tool_table_mirrors_manifest() -> None:
    """The HTML tool table names exactly the manifested tools. (P7-docs-1)"""
    html = (ROOT / "docs" / "index.html").read_text()
    section = html.split('id="tool-reference"')[1].split("</section>")[0]
    rows = re.findall(r"<td><code>(dyvine\.[^<]+)</code></td>", section)
    assert len(rows) == len(set(rows)), "docs tool table has duplicate rows"
    assert sorted(rows) == sorted(_manifest_tools())


def test_docs_index_has_no_hardcoded_counts_or_snapshots() -> None:
    """Drift-prone counts, stamps, and deployment facts stay out. (P7-docs-2/4/5/6)"""
    html = (ROOT / "docs" / "index.html").read_text()
    for stale in (
        "35 tools",
        "35 idempotent",
        "All 35 tools",
        "Last reviewed:",
        "1,619 of 1,631",
        "/root/.hermes",
        "&lt;owner&gt;/dyvine",
        'href="../LICENSE"',
    ):
        assert stale not in html, f"stale docs fragment survived: {stale}"


def test_architecture_page_is_scriptless_prerendered_svg() -> None:
    """No runtime renderer, CDN, or secret-bearing diagram source. (P7-docs-A1)"""
    html = (ROOT / "docs" / "architecture" / "index.html").read_text()
    assert "<script" not in html
    assert "cdn.jsdelivr.net" not in html
    assert html.count("<svg") == 4
    assert "Last reviewed:" not in html
    assert "35 tools" not in html
    diagrams = ROOT / "docs" / "architecture" / "diagrams"
    assert sorted(path.name for path in diagrams.glob("*.mmd")) == [
        "components.mmd",
        "submit-poll.mmd",
    ]
