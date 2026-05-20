"""Skill installer — writes the canonical SKILL bundle into target dirs.

The bundle (SKILL.md + scripts/ + references/) lives at
``src/agy_mcp/_skill_bodies/<target>/...`` and ships as package data
inside the wheel. The leading underscore is deliberate: it marks the
directory as private-to-the-wheel implementation detail, distinct
from the top-level ``skills/`` browsable tree that exists for users
reading the repo on GitHub. Drift between the two is caught by
``tests/test_install_skill_drift.py``.

Targets and scopes:

* ``claude`` — user scope writes to ``~/.claude/skills/``; project
  scope writes to ``<root>/.claude/skills/``.
* ``codex`` — user scope writes to ``~/.agents/skills/``; project
  scope writes to ``<root>/.agents/skills/``.
* ``antigravity`` — user scope writes to ``~/.agy/skills/``
  (wrapper-owned; the agy CLI doesn't load skills yet, but a future
  release that picks this path will find them); project scope writes
  to ``<root>/.antigravity/skills/``.

The wrapper-owned ``~/.agy/`` directory was picked in Phase 7
because the standing rule ``do not write under ~/.gemini/`` rules out
the Antigravity CLI's own state directory, and ``~/.agy/`` is
unambiguous about who created it.

Every write goes through :func:`safe_write_text` with
``verify_under`` anchored to ``Path.home()`` for user scope or the
validated project root, so a TOCTOU swap on any parent component is
detected.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from importlib.resources import files as _pkg_files
from pathlib import Path
from typing import Iterable, Literal

from agy_mcp.safety import SafetyPolicy
from agy_mcp.utils import safe_write_text

SkillTarget = Literal["claude", "codex", "antigravity", "all"]
SkillScope = Literal["user", "project"]


@dataclass(slots=True)
class InstallEntry:
    target: str
    scope: str
    path: str
    overwrote: bool


@dataclass(slots=True)
class InstallResult:
    installed: list[InstallEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "error": self.error,
            "warnings": list(self.warnings),
            "installed": [
                {
                    "target": e.target,
                    "scope": e.scope,
                    "path": e.path,
                    "overwrote": e.overwrote,
                }
                for e in self.installed
            ],
        }


# ---------------------------------------------------------------------------
# Bundle layout
# ---------------------------------------------------------------------------


def _bundle_layout(target: str) -> tuple[str, list[str]]:
    """Return ``(skill_dir_name, [relative_file_paths])`` for ``target``.

    ``skill_dir_name`` is the leaf directory the bundle lands in (e.g.
    ``collaborating-with-antigravity``). The file list is relative to
    that leaf and mirrors the package-data layout under
    ``agy_mcp/_skill_bodies/<target>/``.
    """

    if target in ("claude", "codex"):
        return (
            "collaborating-with-antigravity",
            [
                "SKILL.md",
                "scripts/agy_bridge.py",
                "references/usage.md",
                "references/prompt-patterns.md",
                "references/security.md",
            ],
        )
    if target == "antigravity":
        return (
            "agy-collaboration",
            [
                "SKILL.md",
                "references/collaboration.md",
            ],
        )
    raise ValueError(f"unknown skill target: {target!r}")


def _read_packaged_file(target: str, rel_path: str) -> str:
    """Read ``agy_mcp/_skill_bodies/<target>/<rel_path>`` from the wheel.

    In a non-editable wheel install the file is wheel-immutable. Under
    ``pip install -e .`` / ``uv pip install -e .`` it instead points at
    the user's source checkout, so a hostile working tree feeds hostile
    install content — by design. (Phase 7 R1 sec P3-2.)
    """

    res = _pkg_files("agy_mcp").joinpath("_skill_bodies").joinpath(target)
    for segment in rel_path.split("/"):
        res = res.joinpath(segment)
    return res.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------


# User-scope destinations. ``antigravity`` lives under a wrapper-owned
# directory (``~/.agy/skills/``) because the standing rule "do not
# write under ~/.gemini/" rules out the Antigravity CLI's own state
# directory. (Phase 5 R1 sec P1 deferred to Phase 7.)
_USER_SKILL_DIRS: dict[str, Path] = {
    "claude": Path.home() / ".claude" / "skills",
    "codex": Path.home() / ".agents" / "skills",
    "antigravity": Path.home() / ".agy" / "skills",
}

_PROJECT_SKILL_DIRS: dict[str, Path] = {
    "claude": Path(".claude") / "skills",
    "codex": Path(".agents") / "skills",
    "antigravity": Path(".antigravity") / "skills",
}


def _expand_targets(targets: Iterable[SkillTarget]) -> list[str]:
    """Expand ``"all"`` to ``["claude", "codex", "antigravity"]`` now that
    every target has a documented destination."""

    out: list[str] = []
    for t in targets:
        if t == "all":
            out.extend(["claude", "codex", "antigravity"])
        elif t in _USER_SKILL_DIRS:
            out.append(t)
        else:
            raise ValueError(f"unknown skill target: {t!r}")
    seen: set[str] = set()
    deduped: list[str] = []
    for t in out:
        if t not in seen:
            deduped.append(t)
            seen.add(t)
    return deduped


def _validate_project_root(project_root: Path) -> Path:
    """Resolve ``project_root`` and reject obvious symlink trickery.

    Concretely checks:

    * the path resolves to an existing directory (``resolve(strict=True)``),
    * the resolved path is a directory,
    * the **leaf** of the user-supplied path is not a symlink.

    Ancestor symlinks (``/tmp/...`` → ``/private/tmp/...``,
    ``/var/...`` → ``/private/var/...``) are deliberately **not**
    rejected here: walking the user-supplied path's parents with
    ``is_symlink()`` would refuse any path under ``/tmp`` on macOS
    (every test fixture and most developer workflows), which is
    not the intent. The escape path the security model actually
    defends is the gap between input validation and the file
    write: that boundary is closed by
    :func:`agy_mcp.utils.safe_write_text`, which is invoked with
    ``verify_under=<resolved project root>`` and walks every
    intermediate component from that resolved root down to the
    destination file with ``is_symlink()``, pre- AND post-rename.
    (Phase 7 R1 sec P2-2 — accepted the docstring-only variant
    the reviewer offered.)
    """

    user_path = project_root.expanduser()
    try:
        resolved = user_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            f"project_root does not resolve to a real path: {project_root}: {exc}",
        ) from exc
    if not resolved.is_dir():
        raise ValueError(f"project_root is not a directory: {project_root}")
    if user_path.is_symlink():
        raise ValueError(f"project_root is a symlink, refusing: {project_root}")
    return resolved


def _resolve_target_dir(target: str, scope: str, project_root: Path | None) -> Path:
    if scope == "user":
        if target not in _USER_SKILL_DIRS:
            raise ValueError(f"unknown user-scope target: {target!r}")
        return _USER_SKILL_DIRS[target]
    if project_root is None:
        raise ValueError("project_root required for project scope")
    if target not in _PROJECT_SKILL_DIRS:
        raise ValueError(f"unknown project-scope target: {target!r}")
    return project_root / _PROJECT_SKILL_DIRS[target]


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def install_skills(
    *,
    targets: Iterable[SkillTarget] = ("all",),
    scope: SkillScope = "user",
    project_root: Path | None = None,
    safety: SafetyPolicy | None = None,
    force: bool = False,
) -> InstallResult:
    """Write the full skill bundle to each requested target.

    ``project_root`` is required when ``scope="project"``. ``safety`` is
    used to redact paths in the returned envelope. ``force=False`` (the
    default) writes a file only if it doesn't already exist or its
    contents differ; ``force=True`` always re-writes.
    """

    sft = safety or SafetyPolicy()
    if scope not in ("user", "project"):
        return InstallResult(error=sft.redact(f"scope must be 'user' or 'project', got {scope!r}"))
    try:
        chosen = _expand_targets(targets)
    except ValueError as exc:
        return InstallResult(error=sft.redact(str(exc)))

    validated_root: Path | None = None
    if scope == "project":
        if project_root is None:
            return InstallResult(error=sft.redact("scope='project' requires project_root"))
        try:
            validated_root = _validate_project_root(project_root)
        except ValueError as exc:
            return InstallResult(error=sft.redact(str(exc)))

    # Pre-resolve the verify_under anchor — same for every file in the
    # call (per scope) so we don't lstat home dir N times.
    anchor: Path | None
    if scope == "project":
        anchor = validated_root
    else:
        try:
            anchor = Path.home().resolve(strict=True)
        except OSError:
            anchor = None

    result = InstallResult()

    # Phase 7 R1 P3.19: warn if a project-scope install would land
    # inside the agy-mcp repo itself. Happens when a tester / reviewer
    # accidentally calls ``install_skills(project_root="/path/to/agy-mcp")``;
    # we still let it proceed (drift tests need to compare on-disk
    # results to the canonical tree) but surface the situation so the
    # accidental writes are obvious.
    if scope == "project" and validated_root is not None:
        try:
            self_marker = Path(__file__).resolve().parents[2]  # src/agy_mcp/ -> repo root
        except (OSError, IndexError):
            self_marker = None
        if self_marker is not None and self_marker == validated_root:
            result.warnings.append(sft.redact(
                f"project_root={validated_root} matches agy-mcp's own source tree; "
                "install will write into the wrapper repo itself"
            ))

    for target in chosen:
        # Snapshot the install index BEFORE this target's loop so the
        # partial-failure cleanup at the bottom only drops the entries
        # this iteration appended — earlier targets' successful
        # ``overwrote=False`` rows (idempotent re-installs that happen
        # to share the call with a failing target) must survive.
        # Phase 7 R1 arch P2-1.
        installed_before = len(result.installed)
        try:
            target_dir = _resolve_target_dir(target, scope, validated_root)
        except ValueError as exc:
            result.warnings.append(sft.redact(str(exc)))
            continue
        try:
            skill_dir_name, files = _bundle_layout(target)
        except ValueError as exc:
            result.warnings.append(sft.redact(str(exc)))
            continue
        skill_dir = target_dir / skill_dir_name
        if scope == "project":
            # Containment check: the actual leaf we're about to write
            # under (``skill_dir``) must resolve inside
            # ``validated_root``. Resolving the parent (``target_dir``)
            # is correct today because ``skill_dir_name`` is a
            # hard-coded literal in ``_bundle_layout`` — but a future
            # refactor that lets the leaf become target-driven would
            # silently miss a ``../`` escape one level deeper. Phase 7
            # R1 sec P2-1.
            assert validated_root is not None
            try:
                resolved_skill_dir = skill_dir.resolve()
                resolved_skill_dir.relative_to(validated_root)
            except (OSError, ValueError):
                result.warnings.append(
                    sft.redact(f"skill dir {skill_dir} escapes project_root"),
                )
                continue
            # Refuse to write through a pre-existing symlinked skill_dir.
            if skill_dir.exists() and skill_dir.is_symlink():
                result.warnings.append(
                    sft.redact(f"refusing to write through symlinked skill_dir {skill_dir}"),
                )
                continue

        wrote_any_file = False
        any_failure = False
        for rel_path in files:
            try:
                body = _read_packaged_file(target, rel_path)
            except (FileNotFoundError, OSError) as exc:
                result.warnings.append(
                    sft.redact(f"missing bundle file {target}/{rel_path}: {exc}"),
                )
                any_failure = True
                continue
            dest = skill_dir / rel_path
            try:
                overwrote = dest.exists() or dest.is_symlink()
                if (
                    not force
                    and overwrote
                    and not dest.is_symlink()
                    and dest.read_text(encoding="utf-8") == body
                ):
                    # Skip unchanged file; still record as installed so
                    # callers can audit what landed on disk.
                    result.installed.append(
                        InstallEntry(
                            target=target,
                            scope=scope,
                            path=sft.redact(str(dest)),
                            overwrote=False,
                        )
                    )
                    continue
                safe_write_text(
                    dest, body, mode=0o644, verify_under=anchor,
                )
                wrote_any_file = True
            except OSError as exc:
                result.warnings.append(
                    sft.redact(f"install to {dest} failed: {exc}"),
                )
                any_failure = True
                continue
            result.installed.append(
                InstallEntry(
                    target=target,
                    scope=scope,
                    path=sft.redact(str(dest)),
                    overwrote=overwrote,
                )
            )
        if any_failure and not wrote_any_file:
            # Drop entries this target appended (skipped-unchanged rows
            # for files we DID get through before the failure). Earlier
            # targets' entries survive because we snapshotted the index
            # at the top of the loop.
            del result.installed[installed_before:]

    if not result.installed:
        if result.warnings:
            result.error = sft.redact("no targets installed (see warnings)")
        else:
            result.error = sft.redact("no targets installed")
    return result


# ---------------------------------------------------------------------------
# CLI entry point — used by the ``agy-install-skill`` console script.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Argparse-driven CLI shim around :func:`install_skills`."""

    import argparse

    parser = argparse.ArgumentParser(
        prog="agy-install-skill",
        description=(
            "Install the agy-mcp collaboration skill bundle into one or "
            "more agent platforms (Claude, Codex, Antigravity)."
        ),
    )
    parser.add_argument(
        "--target",
        action="append",
        default=None,
        choices=["claude", "codex", "antigravity", "all"],
        help="Repeatable. Default: --target all.",
    )
    parser.add_argument(
        "--scope", default="user", choices=["user", "project"],
        help="user (default) or project; project requires --project-root.",
    )
    parser.add_argument(
        "--project-root", type=Path, default=None,
        help="Required when --scope=project. The repo root the bundle lands under.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite even if the on-disk body already matches.",
    )
    parser.add_argument(
        "--list-targets", action="store_true",
        help="List supported targets and their resolved paths, then exit.",
    )
    args = parser.parse_args(argv)

    if args.list_targets:
        for t, p in sorted(_USER_SKILL_DIRS.items()):
            skill_dir_name, _ = _bundle_layout(t)
            print(f"user/{t}: {p}/{skill_dir_name}/")
        for t, p in sorted(_PROJECT_SKILL_DIRS.items()):
            skill_dir_name, _ = _bundle_layout(t)
            print(f"project/{t}: <root>/{p}/{skill_dir_name}/")
        return 0

    targets: list[SkillTarget] = args.target or ["all"]
    res = install_skills(
        targets=targets,
        scope=args.scope,
        project_root=args.project_root,
        force=args.force,
    )
    for entry in res.installed:
        print(f"installed: {entry.target} {entry.scope} {entry.path}")
    for w in res.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if res.error:
        print(f"error: {res.error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))


__all__ = [
    "InstallEntry",
    "InstallResult",
    "SkillScope",
    "SkillTarget",
    "install_skills",
    "main",
]
