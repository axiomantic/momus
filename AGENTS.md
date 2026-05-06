# AGENTS.md

Working notes for AI assistants and human contributors collaborating
on the momus codebase.

## Build, test, run

- Python: `uv sync` then `uv run pytest tests/`. Default config skips
  the adversarial corpus.
- TypeScript extensions: `bun test momus/extensions/readonly-tools.test.ts`.
- Pi runtime: pinned to `@mariozechner/pi-coding-agent@0.72.1` via
  `package-lock.json`. Install with `npm ci`, not `npm install`. The
  pin is enforced by the unit checks in
  `tests/integration/test_pi_tool_enforcement.py`
  (`test_pi_version_pinned_to_0_72_1` and
  `test_package_lock_resolves_pi_to_exact_version`).
- End-to-end pi-tool enforcement: opt-in
  `uv run pytest tests/integration/test_pi_tool_enforcement.py -m integration`
  with `LLM_API_KEY` set.

## Adversarial corpus

The repo carries a fixture-based adversarial corpus under
`tests/adversarial/cases/` that exercises six attacker goals (G1-G4)
plus a smoke fixture. Each fixture is pure data: `diff.patch`,
`conventions.md`, `prior-threads.json`, `pr-meta.json`,
`expected.yaml`, `notes.md`.

- Run locally: `uv run pytest -m adversarial`. The smoke case runs
  with `MOMUS_REDTEAM_MOCK_PI=1` and no real LLM call. The other
  cases require `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` and skip
  gracefully without them.
- Cost: bounded upstream by the LLM provider's per-key budget (e.g.,
  OpenRouter); momus does not enforce a local USD cap.
- Per-PR runs: NOT enabled (cost). Manual via
  `gh workflow run redteam-corpus.yml`.
- Weekly cron: `.github/workflows/redteam-corpus.yml` (Mon 06:00 UTC)
  uploads `tests/adversarial/.last-run.json` as a workflow artifact.
- Harness internals: see `tests/adversarial/README.md` for the
  fixture format and the assertion-kind whitelist.

## Architecture overview

- `momus/render.py`: phase-1/2/3 prompt assembly. Untrusted inputs
  (diff, prior threads, conventions) are inlined as fenced data
  rather than path-loaded by the LLM.
- `momus/invoke_pi.py`: spawns pi with the contained-tools allowlist
  and a default-deny env allowlist (escape hatch:
  `MOMUS_PI_ENV_PASSTHROUGH`).
- `momus/extensions/readonly-tools.ts`: TypeScript pi extension
  registering `read_repo`, `grep_repo`, `find_repo`, `ls_repo`,
  `bash_ro`, `write_output`. Cwd containment + realpath checks +
  symlink hardening.
- `momus/findings_schema.py`: Pydantic v2 model for findings.json.
  `extra='forbid'` everywhere; length caps on every text field.
- `momus/publish.py`: redacts credential-shaped strings and off-domain
  image URLs from finding text before posting to GitHub. Validation
  happens at construction time so the redaction holds across 422
  retry branches.
- `momus/prompts/`: phase prompts. The phase-2 prompt renders an
  example finding via `<<CALIBRATION_FIELD>>` substitution in
  `momus/render.py`; the substitution emits `"calibration": {...}`
  (dict shape) to match the Pydantic schema. A previous string-typed
  example was fixed in this branch (commit `86e9e59`).

## Conventions

- No em-dashes in prose (commit messages, docs, comments). The
  `tests/adversarial/cases/*/diff.patch` and `prior-threads.json`
  attacker payloads may contain em-dashes because they are data
  representing what an attacker sends.
- No AI-attribution footers in commits or PR descriptions.
- Atomic commits: one task per commit per the implementation plan.

## Testing conventions: pytest-tripwire

`pytest-tripwire` (dev dep, version pinned in `pyproject.toml`) is the
preferred mocking layer for tests that verify GitHub API payloads,
subprocess invocations, or any other call where **call ordering, exact
argument shape, and number-of-calls all matter for correctness**. Plain
`unittest.mock.patch` / `pytest`'s `monkeypatch` capture a function and
let the test grep through a list of calls afterward. Tripwire enforces
the timeline strictly: every queued return / raise must fire, every
fired interaction must be asserted, and the assertions must match in
order. Examples already in tree: `tests/test_status.py`,
`tests/test_invoke_pi.py`, `tests/test_publish.py`,
`tests/test_publish_validation.py`,
`tests/test_publish_retry_redaction.py`.

**When to reach for tripwire**

- A code path posts to a remote API (GitHub, gh CLI) and the test wants
  to pin the exact endpoint + JSON body shape.
- A code path retries on a specific error (e.g. publish.py's 422
  branches) and the test wants to verify the retry payload, not just
  the final state.
- A code path is supposed to call a helper exactly N times in a
  specific order (e.g. invoke_pi's first-call vs retry).
- The current test relies on a captured-list-and-grep pattern that
  would silently pass if the SUT started calling the mock with a
  different shape.

**When NOT to reach for tripwire**

- The test does not mock anything (pure unit test on a data
  transformation: see `tests/test_progress.py`,
  `tests/test_preflight.py`).
- The test only patches a fixture and never asserts on calls
  (e.g. `monkeypatch.setenv`).
- The test patches at module scope where tripwire's `with tripwire:`
  context would create scoping noise.

**Canonical pattern**

```python
import tripwire
from momus import publish as publish_mod

def test_publish_posts_review_once():
    # `doc`, `pr_meta`, `cfg`, and `expected_payload` are assumed to be
    # fixtures or locals defined elsewhere in the test module — this
    # snippet shows only the tripwire-relevant scaffolding.
    gh_api = tripwire.mock.object(publish_mod, "_gh_api")
    gh_api.returns({})  # one queued return per expected call

    with tripwire:
        publish_mod.publish(doc, [], pr_meta, cfg, run_url="https://run/1")

    gh_api.assert_call(
        args=("POST", "/repos/owner/repo/pulls/25/reviews", expected_payload),
        kwargs={},
    )
```

For sequenced behavior, chain `.raises(err).returns({})` to queue an
exception then a success; assert each in order with two `assert_call`
calls. Use `dirty_equals` matchers (`IsInstance`, `IsDict(...,
partial=True)`, `IsStr(regex=...)`) inside `assert_call` when the exact
value is unstable but the shape matters. `assert_call` accepts a
`raised=` kwarg to match calls that propagated an exception.

**Upstream:**
[pytest-tripwire on PyPI](https://pypi.org/project/pytest-tripwire/).
The installed package's top-level `tripwire` module is the source of
truth: `uv run python -c "import tripwire; help(tripwire)"` and
`help(tripwire.mock)` print the canonical API.

## Common gotchas

- Pi's `--tools` allowlist excludes `read`, `grep`, `find`, `ls` in
  favor of the `*_repo` variants; LLM prompts must use the new names.
- The bot's process env is scrubbed before pi starts. Adding a new
  required env var means updating the allowlist in
  `momus/invoke_pi.py`, not just the workflow YAML.
- The findings.json schema uses unprefixed `calibration` (Pydantic v2
  reserves underscore-prefixed names for private attributes); render-
  layer was renamed in commit `2c9ccdb`.
