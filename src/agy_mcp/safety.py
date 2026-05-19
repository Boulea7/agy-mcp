"""Safety policy: env scrubbing, command deny-list, write/worktree gates.

This module never raises on malformed input — its job is to be a defensive
last-resort filter. All decisions return :class:`SafetyDecision` so callers
can log and surface the reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from agy_mcp.config import Config, SafetyConfig
from agy_mcp.models import BridgeRequest, Mode
from agy_mcp.utils import redact_command, redact_text, scrub_env

# ---------------------------------------------------------------------------
# Always-on env name scrub list. SafetyConfig.scrub_extra_env extends this.
# ---------------------------------------------------------------------------

DEFAULT_SCRUB_ENV_NAMES: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PAT",
    "GITLAB_TOKEN",
    "HF_TOKEN",
    "HUGGINGFACE_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AZURE_OPENAI_API_KEY",
    "AZURE_CLIENT_SECRET",
    "VERTEX_AI_API_KEY",
    "DATABRICKS_TOKEN",
    "STRIPE_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_USER_TOKEN",
    "NPM_TOKEN",
    "PYPI_TOKEN",
    "DATABASE_URL",
    "DATABASE_URI",
    "REDIS_URL",
    "MONGODB_URI",
    "POSTGRES_URL",
    "KUBECONFIG",
    "SENTRY_DSN",
    "VAULT_TOKEN",
    "KAGGLE_KEY",
)

# Substrings inside argv that warrant an alert; "destructive" gets blocked
# unconditionally, "suspicious" gets surfaced as a warning. Patterns must not
# use end-of-string anchors — the screened text can contain arbitrary
# preamble/postamble from the model.
_DESTRUCTIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?im)\brm\s+(?:-[rRfF]+\s+|--recursive\s+|--force\s+)+(?:/|~|\$HOME)"),
    re.compile(r"(?im)\bsudo\s+rm\b"),
    re.compile(r"(?im)\bchmod\s+-?R?\s*777\b"),
    re.compile(r"(?im)\bmkfs\.[a-z0-9]+"),
    re.compile(r"(?im)\bdd\s+if=/dev/(zero|random|urandom)\s+of=/dev/"),
    re.compile(r"(?im)>\s*/dev/sd[a-z]"),
    re.compile(r"(?im):\(\)\s*\{\s*:\|:&\s*\}\s*;:"),  # fork bomb
)

_SUSPICIOUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?im)\bcurl\b[^|]*\|\s*(sh|bash)\b"),
    re.compile(r"(?im)\bwget\b[^|]*\|\s*(sh|bash)\b"),
)

# Sensitive read surfaces — blocked in execute+allow_write, warned otherwise.
_SENSITIVE_READ_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?im)/\.ssh/"),
    re.compile(r"(?im)/\.aws/credentials"),
    re.compile(r"(?im)/\.config/(gcloud|gh|git/credentials)"),
    re.compile(r"(?im)/\.gnupg/"),
    re.compile(r"(?im)keychain"),
    re.compile(r"(?im)Library/Cookies"),
    re.compile(r"(?im)Cookies\.binarycookies"),
)


@dataclass(slots=True)
class SafetyDecision:
    allowed: bool
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SafetyPolicy:
    """Bundled policy applied across the bridge / supervisor / adapter layers."""

    config: SafetyConfig = field(default_factory=SafetyConfig)

    @classmethod
    def from_config(cls, config: Config | None = None) -> "SafetyPolicy":
        if config is None:
            from agy_mcp.config import get_config

            config = get_config()
        return cls(config=config.safety)

    # ------------------------------------------------------------------
    # Env / argv scrubbing
    # ------------------------------------------------------------------

    def scrub_environment(self, env: dict[str, str]) -> dict[str, str]:
        names = (*DEFAULT_SCRUB_ENV_NAMES, *self.config.scrub_extra_env)
        return scrub_env(env, extra_names=names)

    def redact(self, text: str) -> str:
        patterns = tuple(re.compile(p) for p in self.config.redact_extra_patterns)
        return redact_text(text, extra_patterns=patterns)

    def redact_command(self, argv: list[str]) -> list[str]:
        patterns = tuple(re.compile(p) for p in self.config.redact_extra_patterns)
        return redact_command(argv, extra_patterns=patterns)

    # ------------------------------------------------------------------
    # Command / prompt screening
    # ------------------------------------------------------------------

    def screen_prompt(self, prompt: str, *, execute_mode: bool = False) -> SafetyDecision:
        """Check a free-text prompt for obvious destructive patterns.

        ``execute_mode`` upgrades sensitive-read patterns (~/.ssh, ~/.aws,
        keychain, browser cookies) from warning to outright block, since
        those surfaces are catastrophic when combined with write permission.
        """

        for pat in _DESTRUCTIVE_PATTERNS:
            if pat.search(prompt):
                return SafetyDecision(
                    allowed=False,
                    reason=f"prompt contains destructive pattern: {pat.pattern!r}",
                )

        warnings: list[str] = []
        for pat in _SUSPICIOUS_PATTERNS:
            if pat.search(prompt):
                warnings.append(f"prompt mentions sensitive surface: {pat.pattern!r}")

        for pat in _SENSITIVE_READ_PATTERNS:
            if pat.search(prompt):
                if execute_mode:
                    return SafetyDecision(
                        allowed=False,
                        reason=(
                            f"prompt references sensitive read surface ({pat.pattern!r}) "
                            "in execute mode — blocked. Read the file manually and paste "
                            "only the necessary excerpt."
                        ),
                        warnings=warnings,
                    )
                warnings.append(f"prompt references sensitive read surface: {pat.pattern!r}")

        for token in self.config.denylist_extra:
            if token and token in prompt:
                # Never echo the denylist token itself in the rejection reason —
                # the token may itself be a secret-shaped string the user wants
                # to keep out of logs.
                return SafetyDecision(
                    allowed=False,
                    reason="prompt matches project denylist token (token elided from reason)",
                )

        return SafetyDecision(allowed=True, warnings=warnings)

    # ------------------------------------------------------------------
    # Write / worktree gating for execute mode
    # ------------------------------------------------------------------

    def gate_request(
        self,
        request: BridgeRequest,
        *,
        worktree_default: bool,
        is_git_workspace: bool,
        cwd: Path,
    ) -> SafetyDecision:
        """Decide whether a request is safe to run as-issued.

        The bridge layer is expected to honour the returned ``allowed`` flag and
        surface the reason via the BridgeResponse error envelope.
        """

        warnings: list[str] = []

        execute_with_write = request.mode == "execute" and request.allow_write
        prompt_decision = self.screen_prompt(request.prompt, execute_mode=execute_with_write)
        if not prompt_decision.allowed:
            return prompt_decision
        warnings.extend(prompt_decision.warnings)

        write_required = _mode_writes(request.mode)
        if write_required and not request.allow_write:
            return SafetyDecision(
                allowed=False,
                reason=(
                    f"mode={request.mode!r} requires explicit allow_write=True; "
                    "pass --allow-write or set AGY_MCP_ALLOW_WRITE_DEFAULT=1."
                ),
                warnings=warnings,
            )

        if request.mode == "execute" and request.allow_write:
            wants_worktree = request.worktree if request.worktree is not None else worktree_default
            if wants_worktree and not is_git_workspace:
                warnings.append(
                    "worktree=True but cwd is not a git repository; "
                    "the bridge will refuse to mutate a non-git tree."
                )
            if not wants_worktree:
                warnings.append(
                    "execute mode is writing directly to the workspace; "
                    "set worktree=True or AGY_MCP_WORKTREE_DEFAULT=1 to isolate."
                )

        if request.mode != "execute" and request.allow_write:
            warnings.append(
                f"allow_write=True ignored for mode={request.mode!r}; only execute writes."
            )

        return SafetyDecision(allowed=True, warnings=warnings)


def _mode_writes(mode: Mode) -> bool:
    """``execute`` is the only mode permitted to mutate the workspace."""

    return mode == "execute"


def is_git_workspace(cwd: Path, *, max_climb: int = 6) -> bool:
    """Return True when ``cwd`` (or an ancestor up to ``max_climb`` levels) has ``.git``.

    The climb is capped so that a stray ``~/.git`` (a common dev-laptop quirk
    where ``git init`` was once run in $HOME) does not falsely classify every
    subdirectory under home as a git workspace, defeating the worktree
    isolation safety net.
    """

    current = cwd.resolve()
    for idx, ancestor in enumerate((current, *current.parents)):
        if idx > max_climb:
            break
        if (ancestor / ".git").exists():
            return True
    return False


__all__ = [
    "DEFAULT_SCRUB_ENV_NAMES",
    "SafetyDecision",
    "SafetyPolicy",
    "is_git_workspace",
]
