"""Tests for package version metadata consistency."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _is_metadata_package_not_found_error(node: ast.expr | None) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "PackageNotFoundError"
        and isinstance(node.value, ast.Name)
        and node.value.id == "metadata"
    )


def _version_fallback_literals(tree: ast.Module) -> list[str]:
    versions: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            if not _is_metadata_package_not_found_error(handler.type):
                continue
            for stmt in handler.body:
                if not isinstance(stmt, ast.Assign):
                    continue
                if not any(
                    isinstance(target, ast.Name) and target.id == "__version__"
                    for target in stmt.targets
                ):
                    continue
                assert isinstance(stmt.value, ast.Constant)
                assert isinstance(stmt.value.value, str)
                versions.append(stmt.value.value)
    return versions


def test_source_checkout_fallback_version_matches_pyproject():
    """Keep the editable/source fallback in sync with the packaged version."""

    pyproject = tomllib.loads(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = pyproject.get("project")
    assert isinstance(project, dict)
    expected = project.get("version")
    assert isinstance(expected, str)
    assert expected

    tree = ast.parse(
        (_REPO_ROOT / "src" / "agy_mcp" / "__init__.py").read_text(encoding="utf-8")
    )
    fallback_versions = _version_fallback_literals(tree)

    assert fallback_versions == [expected]
