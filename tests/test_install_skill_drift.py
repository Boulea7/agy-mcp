"""Package-data drift test for the skill bundles.

The canonical, human-readable skill bundles live at
``skills/<target>/<skill-dir>/``. The installer reads from
``src/agy_mcp/_skill_bodies/<target>/`` (shipped as package data inside
the wheel). This test fails when the two trees drift apart so a SKILL
edit cannot land in one half but not the other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agy_mcp.install import _bundle_layout, _read_packaged_file

REPO_ROOT = Path(__file__).resolve().parent.parent


def _canonical_path(target: str, skill_dir_name: str, rel_path: str) -> Path:
    if target == "claude":
        return REPO_ROOT / "skills" / "claude" / skill_dir_name / rel_path
    if target == "codex":
        return REPO_ROOT / "skills" / "codex" / skill_dir_name / rel_path
    if target == "antigravity":
        return REPO_ROOT / "skills" / "antigravity" / skill_dir_name / rel_path
    raise AssertionError(f"unknown target {target!r}")


@pytest.mark.parametrize("target", ["claude", "codex", "antigravity"])
def test_skill_bundle_package_data_matches_canonical_tree(target: str) -> None:
    """Every file the installer ships must byte-match the human-readable
    copy under ``skills/<target>/``."""

    skill_dir_name, files = _bundle_layout(target)
    for rel_path in files:
        packaged = _read_packaged_file(target, rel_path)
        canonical = _canonical_path(target, skill_dir_name, rel_path)
        assert canonical.is_file(), (
            f"canonical skill file is missing: {canonical} "
            f"(packaged copy exists but cannot be cross-checked)"
        )
        on_disk = canonical.read_text(encoding="utf-8")
        assert packaged == on_disk, (
            f"package-data drift for {target}/{rel_path}: "
            f"copy under src/agy_mcp/_skill_bodies/{target}/{rel_path} "
            f"does not match {canonical}"
        )
