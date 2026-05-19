"""Cross-platform, dependency-free helpers shared by bridge / supervisor / adapters."""

from __future__ import annotations

import os
import re
import sys
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
    r"|access[_-]?key|private[_-]?key|session[_-]?key|auth)"
)

# Token-looking strings: at least 20 chars of base64url, hex, or Bearer-style.
_VALUE_TOKEN = re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|"
                           r"AIza[0-9A-Za-z_-]{30,}|ya29\.[0-9A-Za-z_-]{20,}|"
                           r"[A-Za-z0-9_-]{40,})\b")
_BEARER_HEADER = re.compile(r"(?i)(Bearer\s+)([A-Za-z0-9._\-+/=]{8,})")
_AUTHZ_HEADER = re.compile(r"(?i)(Authorization[:=]\s*)([^\s\"',;]+)")

REDACTION_PLACEHOLDER = "***"


def redact_text(value: str, *, extra_patterns: tuple[re.Pattern[str], ...] = ()) -> str:
    """Redact secret-shaped substrings inside a free-text string.

    Conservative pass: replace bearer tokens, Authorization headers, and long
    high-entropy tokens with ``***``. Extra patterns may be supplied by the
    caller (e.g. project-specific deny-list values).
    """

    if not value:
        return value
    redacted = _BEARER_HEADER.sub(r"\1" + REDACTION_PLACEHOLDER, value)
    redacted = _AUTHZ_HEADER.sub(r"\1" + REDACTION_PLACEHOLDER, redacted)
    redacted = _VALUE_TOKEN.sub(REDACTION_PLACEHOLDER, redacted)
    for pat in extra_patterns:
        redacted = pat.sub(REDACTION_PLACEHOLDER, redacted)
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


def safe_write_text(path: Path, content: str, mode: int = 0o644) -> None:
    """Write ``content`` to ``path`` atomically with restrictive permissions."""

    ensure_directory(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    if not is_windows():
        try:
            tmp.chmod(mode)
        except OSError:
            pass
    os.replace(tmp, path)


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
