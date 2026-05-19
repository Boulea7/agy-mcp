"""Safety policy: env scrubbing, command deny-list, write/worktree gates.

This module never raises on malformed input — its job is to be a defensive
last-resort filter. All decisions return :class:`SafetyDecision` so callers
can log and surface the reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

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
)

# Substrings inside argv that warrant an alert; "destructive" gets blocked
# unconditionally, "suspicious" gets surfaced as a warning.
_DESTRUCTIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\brm\s+-rf\s+/\s*$"),
    re.compile(r"(?i)\brm\s+-rf\s+/[a-z]+/?\s*$"),
    re.compile(r"(?i)\bsudo\s+rm\b"),
    re.compile(r"(?i)\bchmod\s+-?R\s+777\b"),
    re.compile(r"(?i)\bmkfs\."),
    re.compile(r"(?i)\bdd\s+if=/dev/(zero|random)\s+of=/dev/"),
    re.compile(r"(?i)>\s*/dev/sd[a-z]"),
    re.compile(r"(?i):\(\)\s*\{\s*:\|:&\s*\}\s*;:"),  # fork bomb
)

_SUSPICIOUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bcurl\b[^|]*\|\s*(sh|bash)\b"),
    re.compile(r"(?i)\bwget\b[^|]*\|\s*(sh|bash)\b"),
    re.compile(r"(?i)/\.ssh/"),
    re.compile(r"(?i)/\.aws/credentials"),
    re.compile(r"(?i)/\.config/(gcloud|gh|git/credentials)"),
    re.compile(r"(?i)/\.gnupg/"),
    re.compile(r"(?i)/keychain"),
    re.compile(r"(?i)Library/Cookies"),
    re.compile(r"(?i)Cookies\.binarycookies"),
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

    def screen_prompt(self, prompt: str) -> SafetyDecision:
        """Check a free-text prompt for obvious destructive patterns."""

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

        for token in self.config.denylist_extra:
            if token and token in prompt:
                return SafetyDecision(
                    allowed=False,
                    reason=f"prompt matches project denylist token: {token!r}",
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

        prompt_decision = self.screen_prompt(request.prompt)
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


def is_git_workspace(cwd: Path) -> bool:
    """Return True when ``cwd`` (or an ancestor) contains a ``.git`` directory or file."""

    current = cwd.resolve()
    for ancestor in (current, *current.parents):
        if (ancestor / ".git").exists():
            return True
    return False


__all__ = [
    "DEFAULT_SCRUB_ENV_NAMES",
    "SafetyDecision",
    "SafetyPolicy",
    "is_git_workspace",
]
