# Changelog

All notable changes to `agy-mcp` are tracked here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project
uses [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.6] — 2026-05-21

### Fixed

- **`agy --print` silent-failure detection**. `agy v1.0.0` swallows
  upstream API errors (`FAILED_PRECONDITION` / `PERMISSION_DENIED` /
  `UNAUTHENTICATED` / `RESOURCE_EXHAUSTED`, including the geographic-
  restriction `User location is not supported for the API use` returned
  to China-region IPs by `daily-cloudcode-pa.googleapis.com`) — they
  appear only in the klog file with severity `E`, while the process
  exits 0 with empty stdout. The wrapper would therefore mis-report
  `success=true` with an empty `agent_messages`. v0.1.6 adds five
  klog error patterns to `AgyPrintBackend`'s tail reader and a new
  `_RunContext.had_upstream_error` flag; on detection the result
  envelope is promoted to `subtype=upstream_error` and the first
  upstream error message is surfaced via `result.text` so callers
  see the real failure. The composite `agent executor error: <inner>`
  pattern matches first so its structured subtype wins over the inner
  status name. Discovered during the v0.1.5 PyPI smoke test (see
  `docs/review-followups.md` and the LLM Wiki case
  `agy-cli-silent-exit-on-api-error`).

### Documentation

- **`docs/installation.md`**. Switched the recommended `uv tool
  install` command from `--from git+...` to plain `uv tool install
  agy-mcp` now that v0.1.5+ is on PyPI; git+ stays as an explicit
  fallback for unreleased branches. Added a *China mirror lag*
  subsection covering why a freshly published version takes hours
  to a day to appear on tuna / aliyun / sjtu / tencent mirrors and
  the one-shot `UV_INDEX_URL=https://pypi.org/simple/` workaround.
  README.md and docs/README_EN.md Quickstart, plus the bot-install
  prompt's "install source" step, were synced to the same default.

### Added

- **`tests/test_adapters_agy.py` upstream-error coverage**. Four new
  klog-parser tests (`failed_precondition`, `agent_executor_error`,
  `first_upstream_error_only`, `permission_denied`) plus the
  per-pattern subtype assertions raise the suite from 525 → 529
  tests.

## [0.1.5] — 2026-05-21

### Added

- **`agy_purge` MCP tool**. Wraps
  ``SessionStore.purge_older_than`` so an operator can drop aged
  session-store directories without dropping to a shell. Accepts
  ``days`` (positive integer, ≤ 10 years) and returns the redacted
  list of removed job ids plus a coarse remaining count. Refuses
  ``days <= 0`` outright so an off-by-one config can't wipe the
  whole store.
- **`agy_mcp.routing` module**. Canonical home for
  ``select_backend`` / ``build_adapter``. The supervisor now imports
  routing directly instead of lazy-importing the bridge CLI, breaking
  the historical ``bridge ⇄ supervisor`` import cycle. The bridge
  keeps thin module-local aliases (``_build_adapter`` /
  ``_select_backend``) so the test-monkeypatch surface is preserved.
- **`SafetyPolicy.compile_warnings`**. Bad
  ``[safety.redact_extra_patterns]`` entries no longer disappear
  silently — every compile failure surfaces as a
  ``redact_extra_patterns[<index>] failed to compile and was
  dropped: <re.error>`` diagnostic plus a ``logging.warning``. The
  warning never echoes the raw pattern body (it might itself be a
  secret-shaped probe).
- **`agy_mcp/_skill_bodies/` REQUIRED set in the release auditor**.
  ``scripts/check_release_artifacts.py`` now ships a
  ``REQUIRED_WHEEL_FILES`` set covering every runtime module + every
  skill body file, plus a wheel-metadata check that validates
  ``METADATA`` carries ``Name: agy-mcp`` / ``Version:`` and that
  every shipped path is listed in ``RECORD``. A broken hatch build
  can no longer escape CI with a missing ``_skill_bodies/`` payload.
- **`SessionStore(clock=...)` injection**. Tests that previously
  inserted ``time.sleep(0.05)`` between back-to-back ``create_job``
  calls to get distinct ``st_mtime`` ordering now pass a controlled
  clock and skip the sleep entirely. The production path
  (``clock=None``) keeps the OS-default mtime.
- **`test_install_skill_mixed_user_and_project_scopes_no_cross_pollution`**.
  Exercises a user-scope ``agy_install_skill`` followed by a
  project-scope install in the same process to confirm neither call
  leaks into the other's destination tree.

### Security

- **Worktree slug uniqueness**.
  ``supervisor._worktree_slug`` now always appends the job id to the
  worktree slug instead of using ``session_id or job_id``. Two
  concurrent ``agy_start`` calls sharing the same ``session_id``
  (e.g. the supervisor resuming a long conversation) no longer
  collide on the same ``<repo>/.agy-mcp/worktrees/<slug>/`` path and
  fail the second one with ``FileExistsError``.
- **`agy` UUID regex tightened to canonical 8-4-4-4-12**. The
  ``Created conversation`` / ``Print mode: resuming conversation`` /
  ``Starting conversation update stream for`` / ``Rewinding
  conversation`` klog patterns now require the exact canonical UUID
  shape. The prior loose ``[0-9a-fA-F]{8,}(?:-[0-9a-fA-F]{2,})*`` form
  silently captured ``abcd1234-cafe`` from junk like
  ``abcd1234-cafe-extra-nonhex-suffix``; v0.1.5 rejects the line
  outright instead of seeding the session with a sub-UUID prefix.
- **Worktree git subprocess explicit `encoding="utf-8"`**. All five
  ``subprocess.run`` sites in ``worktree.py`` now pin
  ``encoding="utf-8", errors="replace"`` instead of inheriting the
  platform's preferred encoding. Defends against ``UnicodeDecodeError``
  in mixed-locale environments where ``locale.getpreferredencoding()``
  defaults to anything other than UTF-8.

### Changed

- **`Config` singleton cache thread-safe**. ``get_config`` now wraps
  the lazy ``_CACHED = load_config(...)`` write in a
  ``threading.Lock``, with a lock-free fast path for the warm-cache
  case. Two concurrent MCP tool calls touching the module for the
  first time can no longer both trigger a TOML parse on the same
  path.

### Tested

- New regression tests:
  - ``test_klog_parser_rejects_non_canonical_uuid_shape`` /
    ``test_klog_parser_rejects_trailing_uuid_chars`` pin the
    tightened conversation-id regex.
  - ``test_safety_policy_compile_warnings_surfaces_bad_pattern_index``
    + ``test_safety_policy_compile_warnings_clear_after_signature_change``
    cover the new ``SafetyPolicy.compile_warnings`` channel.
  - ``test_worktree_slug_always_carries_job_id_suffix`` /
    ``test_worktree_slug_without_session_falls_back_to_job_id`` /
    ``test_worktree_slug_caps_length_at_80_chars`` lock in the slug
    uniqueness invariant.
  - ``test_injected_clock_pins_job_dir_mtime`` verifies the new
    SessionStore clock seam.
  - ``test_agy_purge_rejects_zero_or_negative`` /
    ``test_agy_purge_rejects_non_integer`` /
    ``test_agy_purge_returns_removed_jobs`` cover the new
    ``agy_purge`` MCP tool surface.
  - ``test_install_skill_mixed_user_and_project_scopes_no_cross_pollution``
    catches any future shared-state leak between user + project
    install scopes.
- ``tests/test_supervisor.py::_init_git_repo`` and
  ``tests/test_bridge.py::_init_git_repo`` now pass
  ``GIT_CONFIG_NOSYSTEM=1``, override ``HOME`` to a per-test scratch
  dir, and pin ``-c init.defaultBranch=main -c commit.gpgsign=false
  -c tag.gpgsign=false`` so the runner's ``~/.gitconfig`` (signing,
  hook paths, commit templates) can't make the suite flaky.
- ``test_list_jobs_returns_newest_first`` / ``test_list_jobs_limit``
  / ``test_find_by_session_id_returns_most_recent`` switched to the
  new injected ``SessionStore(clock=...)`` instead of
  ``time.sleep(0.05)``.
- Full suite: 525 tests, hermetic (``env -i`` PATH stripped of
  ``agy``/``gemini``).

## [0.1.4] — 2026-05-21

### Fixed

- **CI: ``astral-sh/setup-uv`` floating tag**. v0.1.3 bumped the
  action from ``@v3`` to ``@v8``, but ``setup-uv`` (per their v8.0
  release notes) stops publishing floating major tags; ``@v8`` and
  ``@v8.0`` no longer resolve and CI failed at "Set up job" with
  ``Unable to resolve action astral-sh/setup-uv@v8``. Pinned to
  ``@v7`` instead — the most recent floating major tag the project
  still publishes — until we move to an SHA-pinned reference. No
  functional change; v0.1.3 source is identical and remains a valid
  pin if you're already on it (only CI/release-gate are affected).

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

[Unreleased]: https://github.com/Boulea7/agy-mcp/compare/v0.1.5...HEAD
[0.1.5]: https://github.com/Boulea7/agy-mcp/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/Boulea7/agy-mcp/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/Boulea7/agy-mcp/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/Boulea7/agy-mcp/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Boulea7/agy-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Boulea7/agy-mcp/releases/tag/v0.1.0
