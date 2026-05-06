# Changelog

All notable changes to momus are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-05-05

Injection hardening release. Closes the prompt-injection attack
surface surfaced in the design doc at
`docs/design/2026-05-05-momus-injection-hardening.md`. Single PR,
twenty-plus atomic commits, full implementation plan at
`plans/2026-05-05-momus-injection-hardening-impl.md`.

### Added

- **Contained read-only tools**: new pi extension tools
  `read_repo`, `grep_repo`, `find_repo`, `ls_repo`. Each is cwd-
  contained: paths outside the worktree are rejected before the
  filesystem is touched. Replaces pi's built-in `read`, `grep`,
  `find`, `ls`, which can address absolute paths anywhere on the
  runner.
- **Default-deny env allowlist**: `momus/invoke_pi.py` now scrubs the
  process environment before spawning pi. Only a fixed set of
  variables (`HOME`, `PATH`, `LLM_*`, `GITHUB_REPOSITORY`, etc.) is
  forwarded. The escape hatch `MOMUS_PI_ENV_PASSTHROUGH=NAME1,NAME2`
  lets users opt extra variables back in.
- **Pydantic-validated findings.json**: `momus/findings_schema.py`
  declares the wire-format with `extra='forbid'` everywhere and
  length caps on every text field. Validation happens at publish-
  payload construction time, so prompt-injected shape drift fails
  closed instead of leaking to GitHub.
- **Publish-time redaction**: `momus/publish.py` runs
  `redact_for_publish` over every finding before posting. Credential-
  shaped strings (`ghp_`, `sk-`, `AKIA...`) and off-domain image URLs
  are scrubbed. Redaction holds across the GitHub 422-retry branches.
- **Adversarial corpus harness**: `tests/adversarial/` carries six
  attacker-goal fixtures plus a smoke case. Run via
  `pytest -m adversarial` (opt-in by default). Weekly cron at
  `.github/workflows/redteam-corpus.yml` (Monday 06:00 UTC) executes
  the corpus against the configured LLM and uploads results.
- **Phase-1 prompt placeholder**: `<<UNTRUSTED_PRIOR_THREADS_JSON>>`
  inlines prior-threads as fenced data instead of pointing the LLM
  at a file path.
- **Per-tool-call audit log**: `MOMUS_TOOLCALL_LOG` env var, when
  set, causes the readonly-tools extension to emit one JSON record
  per tool invocation. Used by the corpus harness to assert that no
  forbidden paths or commands were touched.
- **Per-PR LLM cost in the review footer**: each review now ends
  with `Cost: $X.YZ ... I in / O out tokens ... model`. The figure
  is summed from pi-ai's per-turn `usage.cost.total` (which it
  computes from its bundled per-Mtok pricing table) and rounded to
  whole cents. The BYO provider registration now pulls the model's
  real cost from pi-ai's registry by id; previously it hardcoded
  zero so cost computation produced $0.00 even when tokens flowed.
  Suppressed entirely when no tokens are observed (e.g. a phase
  aborted before its first request).

### Changed

- **POSSIBLY BREAKING**: pi-coding-agent is pinned to v0.72.1 via
  `package-lock.json`. Use `npm ci`, not `npm install`. Floating to
  `latest` is no longer supported because pi releases occasionally
  alter the tool-event schema, and the behavioral CI test
  (`test_pi_rejects_disallowed_tool`) is calibrated against 0.72.x.
- **POSSIBLY BREAKING**: `gh` is removed from the `bash_ro`
  allowlist. The LLM phases never invoked it; `gh` runs from Python
  pre-pi (`fetch_priors.py`). If your fork relies on the LLM calling
  `gh`, restore it explicitly in your local extension.
- **POSSIBLY BREAKING**: env vars not on the W3 allowlist no longer
  reach pi. Audit any provider-specific or fork-specific variables
  and add them to `MOMUS_PI_ENV_PASSTHROUGH` if needed.
- **TRANSPARENT**: phase 1 prompt now inlines prior-threads as
  fenced data instead of as an unreadable path reference. Existing
  callers see no API change.
- **TRANSPARENT**: phase 2 example finding uses `"calibration":
  {"would_human_block": "...", "rationale": "..."}` to match the
  Pydantic schema (was a string in v1.0.0; the schema was already
  dict-typed but the example was inconsistent).

### Fixed

- Calibration field rendered the wrong type in the prompt example
  (string vs schema-required dict). Real LLM output following the
  example would have failed Pydantic validation. Discovered during
  W5 fact-check.

### Security

- Closes G3 (credential exfil via prior-thread instruction): the
  `*_repo` tools cannot read `/proc/self/environ`, the env allowlist
  scrubs unrelated variables, and `bash_ro`'s argv allowlist excludes
  shell builtins like `env`/`printenv`/`set`.
- Reduces ASR for G1, G2a, G2b, G2c, G4 via fenced-data input
  framing, the phase-3 verify pass, and schema-gated output. ASR
  numbers tracked weekly via `redteam-corpus.yml`.

## [1.0.0] - 2026-04-26

Initial release. See git log before this commit for details.
