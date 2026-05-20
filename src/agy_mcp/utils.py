"""Cross-platform, dependency-free helpers shared by bridge / supervisor / adapters."""

from __future__ import annotations

import os
import re
import secrets
import stat
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
_AWS_ACCESS_KEY_ID = re.compile(
    r"(?<![A-Za-z0-9])(AKIA|ASIA|AROA|AGPA|AIDA|ANPA|ANVA)[0-9A-Z]{16}(?![A-Za-z0-9])"
)
_SLACK_TOKEN = re.compile(r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9-]{10,}(?![A-Za-z0-9])")
_GITHUB_PAT_FG = re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}(?![A-Za-z0-9])")
_VALUE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:gh[opusr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"AIza[0-9A-Za-z_-]{30,}|ya29\.[0-9A-Za-z_-]{20,}|"
    r"[A-Za-z0-9_-]{40,})(?![A-Za-z0-9])"
)
_BEARER_HEADER = re.compile(r"(?i)(Bearer\s+)([A-Za-z0-9._\-+/=]{8,})")
_AUTHZ_HEADER = re.compile(
    r"(?i)\b("
    r"Authorization|X-Api-Key|X-Auth-Token|X-Auth-Key|Api-Key|Apikey"
    r"|Proxy-Authorization|X-Goog-Api-Key|X-OpenAI-Key|X-Anthropic-Key"
    r")(\s*[:=]\s*)([\"']?)([^\s\"',;]+)([\"']?)"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b("
    r"api[_-]?key|token|secret|password|passwd|credential|client[_-]?secret"
    r"|access[_-]?key|private[_-]?key|session[_-]?key|signing[_-]?key"
    r")(\s*[:=]\s*)([\"']?)([^\s\"',;&]+)([\"']?)"
)

REDACTION_PLACEHOLDER = "***"

# Anonymise per-user paths to keep operator usernames + tooling layout out
# of error envelopes (Phase 3 review M3 + R2 N1; widened in Phase 8 R1
# sec P1-1 to also anonymise bare ``/Users/<u>`` with no trailing path
# component). The regex set covers:
#   * Windows native:  C:\Users\<u>\...
#   * Windows long path: \\?\C:\Users\<u>\...
#   * UNC: \\server\share\Users\<u>\...
#   * Mixed / forward-slash form on Windows: C:/Users/<u>/...
#   * POSIX:  /Users/<u>/...   (macOS)
#             /home/<u>/...    (Linux)
#   * Trailing terminator: either ``/`` (followed by more path components),
#     ``\`` (Windows), or end-of-string. The end-of-string anchor is
#     essential — a string ending with ``/Users/<u>`` (no trailing path)
#     would otherwise escape with the username visible.
# Order matters: Windows patterns run BEFORE the POSIX ``/Users/`` rule so
# a string like ``C:/Users/<u>/`` gets the drive prefix stripped together
# with the user segment, rather than leaving a stray ``C:~/`` behind.
_HOME_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Windows: optional ``\\?\`` long-path prefix, drive letter, both \ and /.
    # Trailing component is ``[\\/]`` or end-of-string.
    re.compile(r"(?i)(?:\\\\\?\\)?[A-Z]:[\\/]Users[\\/][^\\/\s\"']+(?:[\\/]|$)"),
    # UNC ``\\server\share\Users\<u>\`` (or end-of-string).
    re.compile(r"(?i)\\\\[^\\/\s\"']+\\[^\\/\s\"']+\\Users\\[^\\/\s\"']+(?:\\|$)"),
    re.compile(r"/Users/[^/\s\"']+(?:/|$)"),
    re.compile(r"/home/[^/\s\"']+(?:/|$)"),
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
    redacted = _AUTHZ_HEADER.sub(
        r"\1\2\3" + REDACTION_PLACEHOLDER + r"\5",
        redacted,
    )
    redacted = _SECRET_ASSIGNMENT.sub(
        r"\1\2\3" + REDACTION_PLACEHOLDER + r"\5",
        redacted,
    )
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

    Windows-only helper. POSIX callers should pass the prompt through ``argv``
    unchanged.
    """

    if not is_windows():
        return value
    return value.translate(_WINDOWS_ESCAPE_TABLE)


def resolve_executable(name_or_path: str | os.PathLike[str]) -> str | None:
    """Locate an executable across Windows/POSIX semantics.

    Honors PATHEXT on Windows; tries ``.exe``, ``.cmd``, ``.bat``, ``.com`` if
    the bare name does not resolve. On Windows we also probe the well-known
    npm global directories (``%APPDATA%/npm``, ``%LOCALAPPDATA%/npm``,
    ``%ProgramFiles%/nodejs``, ``%NPM_CONFIG_PREFIX%``) so that ``gemini``
    installed via ``npm i -g @google/gemini-cli`` resolves without the user
    having to fix PATH manually. Returns the absolute path or ``None``.
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
    # Windows: npm global dirs are routinely missing from PATH on fresh
    # installs. Probe them last so PATH wins when it has a different version.
    name_str = str(name_or_path)
    for base in windows_npm_paths():
        for ext in (".cmd", ".bat", ".exe", ".com"):
            probe = base / f"{name_str}{ext}"
            if probe.is_file():
                return str(probe)
    return None


def windows_npm_paths() -> list[Path]:
    """Return existing npm-global install directories on Windows.

    Probes well-known npm prefixes so npm-shipped ``gemini.cmd`` resolves
    even when the npm prefix isn't on PATH. POSIX returns an empty list
    (no-op).
    """

    if not is_windows():
        return []
    paths: list[Path] = []
    env = os.environ
    prefix = env.get("NPM_CONFIG_PREFIX") or env.get("npm_config_prefix")
    if prefix:
        paths.append(Path(prefix))
    appdata = env.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / "npm")
    localappdata = env.get("LOCALAPPDATA")
    if localappdata:
        paths.append(Path(localappdata) / "npm")
    programfiles = env.get("ProgramFiles")
    if programfiles:
        paths.append(Path(programfiles) / "nodejs")
    # Filter to existing directories so callers can iterate without re-checking.
    return [p for p in paths if p.is_dir()]


def augment_path_env_for_windows(env: dict[str, str]) -> dict[str, str]:
    """Prepend npm-global directories to ``env['PATH']`` on Windows in-place.

    Idempotent and case-insensitive (Windows PATH semantics). POSIX is a no-op.
    Returns the same ``env`` mapping for chaining.
    """

    if not is_windows():
        return env
    path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
    path_entries = [p for p in env.get(path_key, "").split(os.pathsep) if p]
    seen_lower = {p.lower() for p in path_entries}
    for candidate in windows_npm_paths():
        text = str(candidate)
        if text.lower() in seen_lower:
            continue
        path_entries.insert(0, text)
        seen_lower.add(text.lower())
    env[path_key] = os.pathsep.join(path_entries)
    return env


def _cmd_quote(arg: str) -> str:
    """Quote a single argument for cmd.exe consumption (Windows .cmd/.bat).

    We escape ``%`` (env-var expansion) and ``^`` (cmd escape char) BEFORE
    quoting, then quote if the value contains a metachar or whitespace.
    Empty values become explicit empty quotes so cmd.exe sees a positional
    argument.
    """

    if not arg:
        return '""'
    arg = arg.replace("%", "%%").replace("^", "^^")
    if any(c in arg for c in '&|<>()^" \t'):
        escaped = arg.replace('"', '"^""')
        return f'"{escaped}"'
    return arg


def prepare_subprocess_command(
    argv: list[str],
    env: Mapping[str, str],
) -> tuple[list[str] | str, bool]:
    """Wrap ``argv`` for Windows ``.cmd/.bat`` invocation when needed.

    ``subprocess.Popen(argv, shell=False)`` cannot reliably execute a
    ``.cmd``/``.bat`` on Windows because the CreateProcess path expects a
    real PE binary. Upstream wraps the call as
    ``"<COMSPEC>" /d /s /c "<quoted cmdline>"`` and lets Popen spawn that
    string with ``shell=False`` (safe — argv is already fused into the
    string we control, no shell-injection surface beyond what the caller
    already had).

    Returns ``(popen_arg, wrapped)``:
        * POSIX, or Windows targeting ``.exe`` / ``.com`` / bare:
          returns ``argv`` unchanged and ``False``.
        * Windows targeting ``.cmd`` / ``.bat``:
          returns a single string + ``True``.
    """

    if not argv or not is_windows():
        return argv, False
    suffix = Path(argv[0]).suffix.lower()
    if suffix not in (".cmd", ".bat"):
        return argv, False
    cmdline = " ".join(_cmd_quote(a) for a in argv)
    comspec = env.get("COMSPEC", "cmd.exe") or "cmd.exe"
    return f'"{comspec}" /d /s /c "{cmdline}"', True


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
    :mod:`agy_mcp.install` that have already pinned a trusted root.

    When ``verify_under`` is set AND the platform supports the ``openat``
    family (Linux, macOS, BSDs — see :func:`_has_openat_support`) we take an
    **airtight** path: a single ``O_DIRECTORY|O_NOFOLLOW`` fd is acquired on
    the resolved root, every intermediate directory is opened via
    ``openat(dir_fd=...)`` with ``O_NOFOLLOW`` so a swapped parent symlink
    raises ``ELOOP`` mid-walk, the tempfile is created with
    ``O_CREAT|O_EXCL|O_NOFOLLOW`` against the pinned parent fd, and the
    final ``rename`` uses ``src_dir_fd``/``dst_dir_fd`` so no path
    traversal happens after the root was pinned. This closes the TOCTOU
    residue documented in ``docs/review-followups.md`` (Phase 4–Phase 7).

    On Windows or filesystems missing ``openat`` support, we fall back to a
    **narrow** detect-after-the-fact strategy: pre-write walk of every
    parent component via ``is_symlink`` (lstat semantics), then a post-rename
    re-walk that raises ``OSError`` if any parent was swapped during the
    race. The post-walk is an audit signal — by the time it raises, the file
    has already been published under the swapped parent. The airtight path
    above is preferred whenever the platform allows it.
    """

    if verify_under is not None and _has_openat_support():
        _safe_write_text_openat(path, content, mode, verify_under)
        return

    if verify_under is not None:
        _ensure_directory_under_verified_root(path.parent, verify_under)
        _verify_parents_no_symlink(path, verify_under)
    else:
        ensure_directory(path.parent)
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
            # in during the mkstemp/open window. Re-walk the parent chain
            # with ``is_symlink()`` (using lstat semantics, not resolve()
            # which would collapse symlinks pointing back into the root
            # and let the relative_to pass — see Phase 5 R3 security P2)
            # before promoting it.
            _verify_parents_no_symlink(tmp, verify_under)
            try:
                resolved_tmp = tmp.resolve(strict=True)
                resolved_tmp.relative_to(verify_under.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise OSError(
                    f"refusing to publish {path}: tempfile escaped verify_under",
                ) from exc
        os.replace(tmp, path)
        if verify_under is not None:
            # Final paranoia check: post-rename, ensure every parent
            # component is still a real directory (lstat-based) AND the
            # destination remains inside verify_under (resolve-based).
            _verify_parents_no_symlink(path, verify_under)
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


# ---------------------------------------------------------------------------
# Airtight openat() variant — used when verify_under is set on POSIX
# ---------------------------------------------------------------------------


def _has_openat_support() -> bool:
    """Return True iff the platform exposes the full openat family we need.

    Required syscalls and flags (all POSIX):
        * ``os.O_DIRECTORY`` and ``os.O_NOFOLLOW`` constants.
        * ``os.open`` / ``os.mkdir`` / ``os.rename`` / ``os.unlink`` /
          ``os.fchmod`` honour the ``dir_fd=...`` keyword (advertised via
          ``os.supports_dir_fd`` for ``open`` / ``mkdir`` / ``rename`` /
          ``unlink``; ``fchmod`` is always available on POSIX).

    Linux, macOS, *BSD all qualify. Windows does not — Python exposes none
    of these constants there, and ``CreateFileW`` lacks an at-relative form.
    """

    if is_windows():
        return False
    if not (hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")):
        return False
    supports = getattr(os, "supports_dir_fd", set())
    needed = {os.open, os.mkdir, os.rename, os.unlink}
    return needed.issubset(supports)


def _safe_write_text_openat(
    path: Path,
    content: str,
    mode: int,
    verify_under: Path,
) -> None:
    """Airtight TOCTOU-safe write rooted at ``verify_under`` via ``openat``.

    After opening ``verify_under`` once with ``O_DIRECTORY|O_NOFOLLOW``,
    every subsequent syscall takes ``dir_fd=`` so no further path
    resolution happens — the only inode the kernel can land on is the one
    we pinned. Each intermediate directory is opened with ``O_NOFOLLOW``
    so a symlink swapped in mid-walk raises ``ELOOP`` instead of letting
    the write escape the root.
    """

    resolved_root, rel_parts = _relative_parts_under_verified_root(
        path.parent,
        verify_under,
        action="write",
        target=path,
    )

    cloexec = getattr(os, "O_CLOEXEC", 0)
    root_flags = os.O_DIRECTORY | os.O_NOFOLLOW | cloexec
    root_fd = os.open(str(resolved_root), root_flags)
    parent_fd = root_fd
    opened_intermediate: list[int] = []
    try:
        # Walk relative segments via openat(dir_fd=parent_fd).
        for segment in rel_parts:
            try:
                os.mkdir(segment, mode=0o755, dir_fd=parent_fd)
            except FileExistsError:
                pass
            try:
                next_fd = os.open(
                    segment,
                    os.O_DIRECTORY | os.O_NOFOLLOW | cloexec,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                # ELOOP / ENOTDIR — symlink or non-dir at this segment.
                raise OSError(
                    f"refusing to write {path}: parent component {segment!r} "
                    f"is a symlink or non-directory under {verify_under}",
                ) from exc
            if parent_fd is not root_fd:
                opened_intermediate.append(parent_fd)
            parent_fd = next_fd

        # Create the tempfile leaf with O_CREAT|O_EXCL|O_NOFOLLOW against
        # the pinned parent fd — no path traversal possible.
        tmp_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
        create_flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | cloexec
        )
        tmp_fd = os.open(tmp_name, create_flags, mode=mode, dir_fd=parent_fd)
        try:
            # umask may strip our mode bits; force them with fchmod while we
            # still own the fd. Best-effort: filesystems that ignore mode are OK.
            try:
                os.fchmod(tmp_fd, mode)
            except OSError:
                pass
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
                fp.write(content)
            tmp_fd = -1  # ownership transferred to fp
            # Atomic in-directory rename. Both src and dst resolved through
            # the same dir_fd so no symlink chase is possible.
            os.rename(
                tmp_name,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except BaseException:
            if tmp_fd >= 0:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp_name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
    finally:
        for fd in opened_intermediate:
            try:
                os.close(fd)
            except OSError:
                pass
        if parent_fd is not root_fd:
            try:
                os.close(parent_fd)
            except OSError:
                pass
        try:
            os.close(root_fd)
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

    resolved_root, rel_parts = _relative_parts_under_verified_root(
        path.parent,
        root,
        action="write",
        target=path,
    )
    cur = resolved_root
    for segment in rel_parts:
        cur = cur / segment
        if cur.is_symlink():
            raise OSError(
                f"refusing to write {path}: parent component {cur} is a symlink",
            )


def _ensure_directory_under_verified_root(parent: Path, root: Path) -> None:
    """Create ``parent`` under ``root`` without following symlink components."""

    resolved_root, rel_parts = _relative_parts_under_verified_root(
        parent,
        root,
        action="create",
        target=parent,
    )
    cur = resolved_root
    for segment in rel_parts:
        cur = cur / segment
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            os.mkdir(cur, mode=0o755)
            continue
        except OSError as exc:
            raise OSError(f"failed to inspect parent component {cur}") from exc
        if stat.S_ISLNK(st.st_mode):
            raise OSError(
                f"refusing to create {parent}: parent component {cur} is a symlink",
            )
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(
                f"refusing to create {parent}: parent component {cur} is not a directory",
            )


def _relative_parts_under_verified_root(
    parent: Path,
    root: Path,
    *,
    action: str,
    target: Path,
) -> tuple[Path, tuple[str, ...]]:
    """Return lexical parent parts under a resolved root, rejecting traversal."""

    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise OSError(f"verify_under root does not resolve: {root}") from exc
    if not resolved_root.is_dir():
        raise OSError(f"verify_under root is not a directory: {root}")
    try:
        rel = parent.relative_to(resolved_root)
    except ValueError as exc:
        raise OSError(
            f"refusing to {action} {target}: parent {parent} not under {root}",
        ) from exc
    rel_parts = rel.parts
    if any(segment in ("", ".", os.pardir) for segment in rel_parts):
        raise OSError(
            f"refusing to {action} {target}: parent contains traversal segment",
        )
    return resolved_root, rel_parts


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
    "augment_path_env_for_windows",
    "configure_utf8_stdio",
    "ensure_directory",
    "expand_user_path",
    "is_windows",
    "prepare_subprocess_command",
    "redact_command",
    "redact_text",
    "resolve_executable",
    "safe_write_text",
    "scrub_env",
    "truncate_middle",
    "utc_now_iso",
    "windows_escape",
    "windows_npm_paths",
]
