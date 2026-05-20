"""Cross-platform, dependency-free helpers shared by bridge / supervisor / adapters."""

from __future__ import annotations

import os
import re
import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    """Return current UTC time as a sortable ISO-8601 string with Z suffix."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

# Patterns that match either an env var name (used to scrub `os.environ` copies)
# or a substring that looks like a secret value in free text (used to scrub log
# lines, error messages, and command previews). The lists are intentionally
# conservative; callers may extend them via SafetyPolicy.denylist_extra.
SECRET_ENV_NAME_PATTERN = re.compile(
    r"(?i)(token|api[_-]?key|secret|password|passwd|credential|bearer|client[_-]?secret"
    r"|access[_-]?key|private[_-]?key|session[_-]?key|signing[_-]?key|webhook"
    r"|auth|_pat$|^pat_|_dsn$|^dsn_|_otp$|^otp_|_pin$|^pin_|certificate|cert"
    # Mid-word matches: `_key_`, `_token_`, `_secret_`, `_password_` etc. catch
    # composite names like APP_KEY_ID, MY_TOKEN_RAW that prior anchored
    # patterns would miss.
    r"|_(key|token|secret|password|credential|auth)(_|$)"
    r"|^(key|token|secret|password|credential|auth)_"
    r"|database[_-]?(url|uri)|postgres[_-]?(url|uri)|redis[_-]?(url|uri)"
    r"|mongodb[_-]?(url|uri)|mysql[_-]?(url|uri)|kubeconfig)"
)

# Provider-specific token shapes are matched before the generic long-token
# fallback so that short fixed-length tokens (e.g. AWS AKID = 20 chars) are
# still redacted.
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
_AWS_ACCESS_KEY_ID = re.compile(r"\b(AKIA|ASIA|AROA|AGPA|AIDA|ANPA|ANVA)[0-9A-Z]{16}\b")
_SLACK_TOKEN = re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")
_GITHUB_PAT_FG = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")
_VALUE_TOKEN = re.compile(
    r"\b(?:gh[opusr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"AIza[0-9A-Za-z_-]{30,}|ya29\.[0-9A-Za-z_-]{20,}|"
    r"[A-Za-z0-9_-]{40,})\b"
)
_BEARER_HEADER = re.compile(r"(?i)(Bearer\s+)([A-Za-z0-9._\-+/=]{8,})")
_AUTHZ_HEADER = re.compile(
    r"(?i)\b("
    r"Authorization|X-Api-Key|X-Auth-Token|X-Auth-Key|Api-Key|Apikey"
    r"|Proxy-Authorization|X-Goog-Api-Key|X-OpenAI-Key|X-Anthropic-Key"
    r")(\s*[:=]\s*)([^\s\"',;]+)"
)

REDACTION_PLACEHOLDER = "***"

# Anonymise per-user paths to keep operator usernames + tooling layout out
# of error envelopes (Phase 3 review M3 + R2 N1). The regex set covers:
#   * Windows native:  C:\Users\<u>\...
#   * Windows long path: \\?\C:\Users\<u>\...
#   * UNC: \\server\share\Users\<u>\...
#   * Mixed / forward-slash form on Windows: C:/Users/<u>/...
#   * POSIX:  /Users/<u>/...   (macOS)
#             /home/<u>/...    (Linux)
# Order matters: Windows patterns run BEFORE the POSIX ``/Users/`` rule so
# a string like ``C:/Users/<u>/`` gets the drive prefix stripped together
# with the user segment, rather than leaving a stray ``C:~/`` behind.
_HOME_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Windows: optional ``\\?\`` long-path prefix, drive letter, both \ and /.
    re.compile(r"(?i)(?:\\\\\?\\)?[A-Z]:[\\/]Users[\\/][^\\/\s\"']+[\\/]"),
    # UNC ``\\server\share\Users\<u>\``.
    re.compile(r"(?i)\\\\[^\\/\s\"']+\\[^\\/\s\"']+\\Users\\[^\\/\s\"']+\\"),
    re.compile(r"/Users/[^/\s\"']+/"),
    re.compile(r"/home/[^/\s\"']+/"),
)


def anonymise_paths(value: str) -> str:
    """Replace ``/Users/<user>/`` style prefixes with ``~/`` for privacy."""

    if not value:
        return value
    out = value
    for pat in _HOME_PATH_PATTERNS:
        out = pat.sub("~/", out)
    return out


def redact_text(value: str, *, extra_patterns: tuple[re.Pattern[str], ...] = ()) -> str:
    """Redact secret-shaped substrings inside a free-text string.

    Order matters: structural patterns (PEM blocks, JWTs) come first so that
    their internal contents are not partially redacted by the generic token
    regex. ``extra_patterns`` is appended last so callers can extend without
    overriding built-ins. The home-path anonymiser runs last so that any
    paths surviving the token sweep land as ``~/...`` rather than
    ``/Users/<u>/...``.
    """

    if not value:
        return value
    redacted = _PEM_BLOCK.sub(REDACTION_PLACEHOLDER, value)
    redacted = _JWT.sub(REDACTION_PLACEHOLDER, redacted)
    # Bearer first so "Authorization: Bearer <token>" gets its token caught
    # before the AUTHZ_HEADER substitution collapses "Bearer" to "***".
    redacted = _BEARER_HEADER.sub(r"\1" + REDACTION_PLACEHOLDER, redacted)
    redacted = _AUTHZ_HEADER.sub(r"\1\2" + REDACTION_PLACEHOLDER, redacted)
    redacted = _AWS_ACCESS_KEY_ID.sub(REDACTION_PLACEHOLDER, redacted)
    redacted = _SLACK_TOKEN.sub(REDACTION_PLACEHOLDER, redacted)
    redacted = _GITHUB_PAT_FG.sub(REDACTION_PLACEHOLDER, redacted)
    redacted = _VALUE_TOKEN.sub(REDACTION_PLACEHOLDER, redacted)
    for pat in extra_patterns:
        redacted = pat.sub(REDACTION_PLACEHOLDER, redacted)
    # Path anonymisation runs last so secret-shaped tokens inside the path
    # are already redacted by the time we collapse ``/Users/<u>/`` to ``~/``.
    redacted = anonymise_paths(redacted)
    return redacted


def redact_command(argv: list[str], *, extra_patterns: tuple[re.Pattern[str], ...] = ()) -> list[str]:
    """Return a copy of ``argv`` with secret-shaped values redacted in place."""

    return [redact_text(a, extra_patterns=extra_patterns) for a in argv]


def scrub_env(env: Mapping[str, str], *, extra_names: tuple[str, ...] = ()) -> dict[str, str]:
    """Return a copy of ``env`` with secret-named keys replaced by ``***``.

    The original mapping is never mutated. Keys that match
    :data:`SECRET_ENV_NAME_PATTERN` or any name in ``extra_names`` (case-
    insensitive) have their value replaced. Useful when emitting a debug
    snapshot of the wrapper's spawn environment.
    """

    extra_lower = {name.lower() for name in extra_names}
    out: dict[str, str] = {}
    for key, value in env.items():
        if SECRET_ENV_NAME_PATTERN.search(key) or key.lower() in extra_lower:
            out[key] = REDACTION_PLACEHOLDER
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Output truncation
# ---------------------------------------------------------------------------


def truncate_middle(text: str, max_chars: int, marker: str = "\n...[truncated]...\n") -> str:
    """Truncate ``text`` to fit within ``max_chars``, preserving head + tail.

    Useful for stdout/stderr previews where both the prologue and epilogue carry
    diagnostic value (klog headers vs. final assistant answer / error). When the
    text already fits, it is returned unchanged.
    """

    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if len(marker) >= max_chars:
        return marker[:max_chars]
    keep = max_chars - len(marker)
    head = keep // 2
    tail = keep - head
    return f"{text[:head]}{marker}{text[-tail:] if tail else ''}"


# ---------------------------------------------------------------------------
# Windows-aware subprocess helpers
# ---------------------------------------------------------------------------


def is_windows() -> bool:
    return os.name == "nt"


_WINDOWS_ESCAPE_TABLE = str.maketrans(
    {
        "\\": "\\\\",
        '"': '\\"',
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
        "\b": "\\b",
        "\f": "\\f",
        "'": "\\'",
    }
)


def windows_escape(value: str) -> str:
    """Escape control characters so a string can survive a cmd.exe round-trip.

    Mirrors the helper used by upstream/reference; only intended for Windows.
    Callers on POSIX should pass the prompt through ``argv`` unchanged.
    """

    if not is_windows():
        return value
    return value.translate(_WINDOWS_ESCAPE_TABLE)


def resolve_executable(name_or_path: str | os.PathLike[str]) -> str | None:
    """Locate an executable across Windows/POSIX semantics.

    Honors PATHEXT on Windows; tries ``.exe``, ``.cmd``, ``.bat``, ``.com`` if
    the bare name does not resolve. Returns the absolute path or ``None``.
    """

    import shutil

    direct = shutil.which(str(name_or_path))
    if direct:
        return direct
    if not is_windows():
        return None
    candidate = Path(str(name_or_path))
    if candidate.exists():
        return str(candidate.resolve())
    for ext in (".exe", ".cmd", ".bat", ".com"):
        result = shutil.which(str(name_or_path) + ext)
        if result:
            return result
    return None


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def expand_user_path(value: str | os.PathLike[str]) -> Path:
    """``Path(value).expanduser().resolve()`` with consistent semantics."""

    return Path(os.fspath(value)).expanduser().resolve()


def ensure_directory(path: Path, mode: int = 0o755) -> Path:
    """Create ``path`` (and parents) idempotently with restrictive mode."""

    path.mkdir(parents=True, exist_ok=True)
    if not is_windows():
        try:
            path.chmod(mode)
        except OSError:
            # Best-effort: missing perms on a shared mount should not crash.
            pass
    return path


def safe_write_text(
    path: Path,
    content: str,
    mode: int = 0o644,
    *,
    verify_under: Path | None = None,
) -> None:
    """Write ``content`` to ``path`` atomically with restrictive permissions.

    Uses a randomised tempfile in the destination directory so two writers
    racing on the same target do not collide on a predictable ``.tmp`` name,
    and refuses to follow a symlinked tempfile (``O_NOFOLLOW``) so an
    attacker cannot pre-create a symlink to e.g. ``~/.ssh/authorized_keys``.

    ``verify_under`` is a defence-in-depth knob for callers like
    :mod:`agy_mcp.install` that have already pinned a trusted root: when
    provided, every intermediate parent of ``path`` from ``verify_under``
    down to ``path.parent`` is checked with ``is_symlink()`` before the
    write, and the resolved final path must remain under
    ``verify_under``. This closes the TOCTOU window where an attacker
    could swap a parent component (`<root>/.claude` → symlink to
    `/etc`) between input validation and the actual write.
    (Phase 5 R2 security P1-2.)
    """

    ensure_directory(path.parent)
    if verify_under is not None:
        _verify_parents_no_symlink(path, verify_under)
    # NamedTemporaryFile in the destination directory gives us atomic rename
    # semantics + a randomised name. We close immediately and reopen with
    # O_NOFOLLOW (where supported) for symlink-safe writes.
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_name)
    try:
        try:
            os.close(fd)
        except OSError:
            pass
        # Try O_NOFOLLOW path first; on filesystems without it (rare on
        # modern systems but seen on some networked mounts) fall back to a
        # plain write — the tempfile was just created via mkstemp so the
        # symlink-swap window is small but non-zero. See docs/review-followups.md.
        wrote_via_safe = False
        flags = os.O_WRONLY | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            real_fd = os.open(tmp, flags)
        except OSError:
            real_fd = -1
        if real_fd != -1:
            try:
                with os.fdopen(real_fd, "w", encoding="utf-8") as fp:
                    fp.write(content)
                wrote_via_safe = True
            except OSError:
                wrote_via_safe = False
        if not wrote_via_safe:
            tmp.write_text(content, encoding="utf-8")
        if not is_windows():
            try:
                tmp.chmod(mode)
            except OSError:
                pass
        if verify_under is not None:
            # Post-write check: even if every parent component was a real
            # directory pre-write, an attacker could have moved a symlink
            # in during the mkstemp/open window. Confirm the tempfile's
            # realpath is still inside verify_under before promoting it.
            try:
                resolved_tmp = tmp.resolve(strict=True)
                resolved_tmp.relative_to(verify_under.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise OSError(
                    f"refusing to publish {path}: tempfile escaped verify_under",
                ) from exc
        os.replace(tmp, path)
        if verify_under is not None:
            # Final paranoia check: post-rename, ensure the destination is
            # still inside verify_under (catches a swap during os.replace).
            try:
                resolved_path = path.resolve(strict=True)
                resolved_path.relative_to(verify_under.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise OSError(
                    f"refusing to leave {path}: final path escaped verify_under",
                ) from exc
    finally:
        # If replace failed, leave no orphan tmp behind.
        if tmp.exists() and tmp != path:
            try:
                tmp.unlink()
            except OSError:
                pass


def _verify_parents_no_symlink(path: Path, root: Path) -> None:
    """Walk ``path.parent`` up to ``root`` and refuse on any symlink.

    The :func:`safe_write_text` ``verify_under`` knob calls this once per
    write to close the parent-directory TOCTOU window.  ``root`` must be
    a real (already-resolved) directory; we walk down from there, not
    up from the leaf, so an attacker can't trick us by replacing
    components above the trusted root.
    """

    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise OSError(f"verify_under root does not resolve: {root}") from exc
    if not resolved_root.is_dir():
        raise OSError(f"verify_under root is not a directory: {root}")
    # Build the chain of intermediate components from root down to
    # path.parent. ``path.parent.relative_to(resolved_root)`` errors if
    # the parent is not under root at all — exactly what we want.
    try:
        rel = path.parent.relative_to(resolved_root)
    except ValueError as exc:
        raise OSError(
            f"refusing to write {path}: parent {path.parent} not under {root}",
        ) from exc
    cur = resolved_root
    for segment in rel.parts:
        cur = cur / segment
        if cur.is_symlink():
            raise OSError(
                f"refusing to write {path}: parent component {cur} is a symlink",
            )


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------


def configure_utf8_stdio() -> None:
    """Force UTF-8 on stdio (avoid mojibake on Windows / non-UTF-8 locales)."""

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


__all__ = [
    "REDACTION_PLACEHOLDER",
    "SECRET_ENV_NAME_PATTERN",
    "anonymise_paths",
    "configure_utf8_stdio",
    "ensure_directory",
    "expand_user_path",
    "is_windows",
    "redact_command",
    "redact_text",
    "resolve_executable",
    "safe_write_text",
    "scrub_env",
    "truncate_middle",
    "utc_now_iso",
    "windows_escape",
]
