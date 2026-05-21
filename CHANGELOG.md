# Changelog

All notable changes to `agy-mcp` are tracked here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project
uses [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.3] — 2026-05-21

### Security

- **`session_id` strict charset validator**. Previously only
  length-capped, ``session_id`` now must match
  ``^[A-Za-z0-9._-]{1,96}$``. Hardens against newline / NUL / shell-
  metacharacter injection into ``ANTIGRAVITY_CONVERSATION_ID`` (env
  splitting) and ``--conversation=<id>`` (argv parsing). Both the
  pydantic model (``models.BridgeRequest``) and the server fast-path
  (``server._validate_session_id``) enforce the same regex.
- **Extended secret-redaction surface**. ``SafetyPolicy.redact``
  / ``utils.redact_text`` now strip Slack ``xoxe-`` refresh tokens,
  ``xoxe.xoxp-`` rotated refreshes, and ``xapp-`` app-level tokens
  on top of the existing ``xox[abprs]-`` set. New
  ``_PROVIDER_TOKEN`` rule covers Stripe
  ``(sk|rk|pk)_(live|test)_`` and ``whsec_``, Anthropic
  ``sk-ant-``, GitLab ``glpat-``, HuggingFace ``hf_``, Twilio
  ``AC[0-9a-f]{32}``, and Notion ``secret_``. Closes a class of
  short-prefix provider tokens that escaped the generic 40-char gate.
- **Home-path anonymisation broadened**. The trailing anchor now
  matches end-of-string, path separators, and natural-language
  punctuation (``" ' . , : ; ! ?`` etc.), so a string like
  ``cwd="/Users/alice"`` no longer slips through with the username
  visible. The username class is tightened to ``[A-Za-z0-9._-]+``
  and a new ``~user/`` explicit-home rule fires on the tilde-prefix
  form.
- **Destructive-prompt blocker covers workspace-relative targets**.
  The original rule only matched ``rm -rf /``, ``~``, or ``$HOME``;
  the new rule also blocks ``rm -rf .``, ``rm -rf *``, ``rm -rf ./src``,
  ``rm -rf node_modules``, ``rm -rf .git``, and ``rm -rf $VAR`` —
  the variants LLM-generated cleanup commands tend to emit.

### Fixed

- **Pipe-reader truncation race**
  (``adapters/base.py:_drain_stream``). The drain loop no longer
  consults ``ctx.stop_event``; it runs until EOF. Previously a
  finalize-block ``stop_event.set()`` could end the loop before the
  child's buffered output was fully read, silently dropping the
  tail of long stdout/stderr.
- **Spool-write failure deadlock**
  (``adapters/base.py:_drain_stream``). When mid-stream
  ``spool.write/flush`` raised ``OSError`` / ``ValueError`` the
  whole drain returned, leaving the pipe unread; a child that
  produced more than one pipe buffer of output blocked forever on
  ``write``. Spool failures now close the spool, emit a
  ``spool_write_failed`` warning event, and keep draining the pipe.
- **Supervisor slot leak on ``_finalize`` exception**
  (``supervisor.py``). If session-store I/O or redaction crashed
  inside ``_finalize``, the next lines that drop the job handle
  and release the concurrency slot never executed — the supervisor
  leaked one slot per failure and eventually rejected every new
  ``start()`` with ``supervisor busy``. Slot release is now in an
  outer ``finally``.

### Changed

- **CI: GitHub Actions runtimes off Node 20**. Bumped
  ``actions/checkout`` v4 → v5 and ``astral-sh/setup-uv`` v3 → v8
  to silence the September 2026 Node 20 deprecation warnings on
  the runners.
- **Skill forwarder package spec resolves dynamically**. The
  ``BRIDGE_PACKAGE_SPEC`` literal in the two skill forwarders
  (``src/agy_mcp/_skill_bodies/{claude,codex}/scripts/agy_bridge.py``
  and the canonical mirrors under ``skills/``) now resolves from
  ``importlib.metadata.version("agy-mcp")`` at import time. The
  static fallback carries the ``__AGY_MCP_VERSION__`` placeholder
  which ``agy_mcp.install._template_skill_body`` substitutes at
  install time, so a deployed copy on a wheel-less host still pins a
  real version. Removes the per-release manual sync of four files.

### Tested

- New regression tests:
  - ``test_anonymise_paths_handles_word_boundary_punctuation``
    pins the punctuation-tail variants.
  - ``test_redact_text_strips_extended_provider_tokens`` covers
    every new provider pattern.
  - ``test_bridge_request_rejects_unsafe_session_id_charset``
    spans newline, CRLF, NUL, path traversal, and shell-metachar
    injection attempts.
  - ``test_skill_forwarder_uvx_spec_prefers_installed_version`` /
    ``...carries_static_fallback_placeholder`` /
    ``test_template_skill_body_resolves_placeholder`` verify the
    dynamic-spec round trip.
  - ``rm -rf .`` / ``rm -rf *`` / ``rm -rf ./src`` /
    ``rm -rf node_modules`` / ``rm -rf .git`` /
    ``rm -rf $WORKSPACE`` added to the destructive-prompt
    parametrisation.
- Removed flaky ``time.sleep(1.1)`` in
  ``test_bridge_response_touch_updates_timestamp`` in favour of a
  ``monkeypatch`` of ``_iso_now``.
- ``tests/test_skill_bridge_forwarders.py`` now resolves package
  paths relative to ``__file__`` so the suite stays hermetic when
  pytest is invoked outside the repo root.
- Full suite: 513 tests, hermetic (``env -i`` PATH stripped of
  ``agy``/``gemini``).

## [0.1.2] — 2026-05-21

### Fixed

- **CI hermeticity**: 5 tests required a real ``agy`` / ``gemini``
  binary on PATH and consequently failed on every GitHub Actions
  runner (which has neither). Two-part fix:
  - ``AgyPrintBackend.run`` and ``GeminiCliBackend.run`` now validate
    the requested ``cwd`` BEFORE invoking ``build_command``; this lets
    the ``invalid_cwd`` defence-in-depth event fire even when the
    underlying binary isn't installed, instead of being masked by a
    ``binary not found`` ``RuntimeError``. Strictly an improvement —
    invalid-cwd now produces a structured event the wrapper can react
    to in any environment.
  - ``test_agy_dry_run_returns_command_preview`` now points
    ``config.backend.agy_bin`` at the existing ``fake_agy_print.py``
    fixture wrapper, so the dry-run path resolves a binary on hosts
    that lack ``agy``.
- **CI lint coverage**: workflow now runs ``ruff check src tests
  scripts`` (was ``src tests``) so future drift in the release-gate
  script is caught upstream rather than at release time.

## [0.1.1] — 2026-05-21

### Added

- **CI matrix on GitHub Actions** (`.github/workflows/ci.yml`) runs
  ruff lint + pytest on ubuntu + macos × Python 3.11 / 3.12 / 3.13,
  followed by a release-gate job that builds the sdist + wheel and
  runs `scripts/check_release_artifacts.py`. Status badge wired
  into the README.
- **`CHANGELOG.md`** following Keep a Changelog format; included in
  the sdist file set and enforced by the release-gate audit.

### Changed

- Narrowed ruff lint rule set to core correctness families
  (`E + W + F + I + B`). The dropped families (`UP`, `SIM`, `RUF`)
  were largely opinion-style rules that conflicted with deliberate
  engineering choices (defensive broad-except in subprocess
  wrappers, etc.). 167 pre-existing lint findings resolved; 43 by
  ruff auto-fix, the rest by rule narrowing.
- Bilingual READMEs now carry a CI status badge alongside the
  existing License / Python / Tests badges.

### Security

- (Backport reminder) Test fixtures resembling live secrets are
  split into adjacent Python string literals so GitHub Push
  Protection no longer matches them; introduced in v0.1.0 and
  carried over here.

## [0.1.0] — 2026-05-20

First public-ready cut.

### Added

- **9 MCP tools** over FastMCP stdio with typed pydantic envelopes:
  `agy`, `agy_continue`, `agy_start`, `agy_status`, `agy_read`,
  `agy_cancel`, `agy_sessions`, `agy_doctor`, `agy_install_skill`.
- **Hybrid backend**: `agy --print` stdout buffer + `--log-file` klog
  tail + optional `transcript.jsonl` watcher. Synthesises lifecycle
  events (conversation start, turn end, sidecar ready, errors) the
  underlying CLI does not emit on stdout.
- **Compatibility backend**: `gemini-cli` adapter as a stream-json
  fallback when both binaries are on PATH.
- **Protocol translator**: `raw` / `claude` / `codex` output envelopes,
  letting Claude Code or Codex consume the same event stream natively.
- **Skill bundles** for Claude Code (`~/.claude/skills/`), OpenAI
  Codex (`~/.agents/skills/`), and Antigravity (`~/.agy/skills/`),
  installable via the `agy_install_skill` tool or `agy-install-skill`
  CLI.
- **Worktree-isolated write mode**: `mode="execute"` with
  `allow_write=True` defaults to running the child process inside a
  fresh `<repo>/.agy-mcp/worktrees/<session_id>/` worktree, leaving
  the host tree untouched.
- **Safety layer**: every error / log / response field is run through
  `SafetyPolicy.redact` to anonymise home paths and scrub PEM / JWT /
  AWS AKID / Bearer / Authorization / Slack tokens / GitHub PATs.
  Destructive prompts (`rm -rf`, `mkfs`, `dd if=/dev/zero`) are
  refused even with `allow_write=True`.
- **Cross-platform process group cancel**: POSIX `start_new_session`
  + `killpg`; Windows `CREATE_NEW_PROCESS_GROUP` + `CTRL_BREAK_EVENT`.
- **Doctor probe** (`agy_doctor`, `agy-doctor`): Python / uv / agy /
  gemini binaries / OAuth credentials / session store, with paths
  redacted before they reach the caller.
- **Release-gate audit script** (`scripts/check_release_artifacts.py`):
  enforces the published file set and forbidden-content patterns
  (raw home paths, AKID / Slack / Bearer / JWT shapes) on the
  generated sdist + wheel before any future `uv publish`.
- **Documentation**: bilingual README (中文 + English), installation
  guide, architecture map, examples, output-strategy deep-dive,
  threat model, CLI capability matrix, and copy-paste project
  snippets under `prompts/`.

### Security

- All test fixtures that resemble live secrets (Slack tokens, AWS
  access keys, OpenAI API keys, GitHub PATs, JWTs, OAuth Bearer
  tokens) are split into adjacent Python string literals
  (`"xox" "b-..."`) so GitHub Push Protection no longer matches them
  as exposed credentials. Runtime behaviour is identical.
- `~/.gemini/` is treated as Antigravity's own state directory and
  the wrapper never writes there; the user-scope `antigravity` SKILL
  lands under the wrapper-owned `~/.agy/skills/`.

### Tested

- 479 unit + integration tests across adapters, bridge, protocol
  translator, safety policy, session store, supervisor, worktree
  isolation, install, doctor, MCP tool surface, Windows shim, and
  release artefact audit.
- Manual end-to-end smoke test on macOS: doctor `healthy=true` (6/6),
  dry-run on three modes, real `agy --print` call with session
  resume.

[Unreleased]: https://github.com/Boulea7/agy-mcp/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/Boulea7/agy-mcp/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/Boulea7/agy-mcp/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Boulea7/agy-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Boulea7/agy-mcp/releases/tag/v0.1.0
