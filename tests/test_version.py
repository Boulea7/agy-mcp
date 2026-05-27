"""Tests for package version metadata consistency."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_source_checkout_fallback_version_matches_pyproject():
    """Keep the editable/source fallback in sync with the packaged version."""

    pyproject = tomllib.loads(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    expected = pyproject["project"]["version"]

    tree = ast.parse(
        (_REPO_ROOT / "src" / "agy_mcp" / "__init__.py").read_text(encoding="utf-8")
    )
    fallback_versions = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.count(".") == 2
    ]

    assert expected in fallback_versions
