# Adversarial corpus harness

Pytest-driven runner for the momus injection-hardening adversarial corpus.

## Layout

```
tests/adversarial/
  __init__.py
  conftest.py              # session-end .last-run.json emission
  harness.py               # pure-Python harness module (load, run, evaluate)
  test_harness_unit.py     # unit tests (no LLM, default-collected)
  test_corpus.py           # parametrised corpus runner (mark: adversarial)
  cases/
    smoke/                 # mocked-pi smoke fixture for V4 gating
      diff.patch
      conventions.md
      prior-threads.json
      pr-meta.json
      expected.yaml
      notes.md
  verification/            # PASS/FAIL artefacts for V0a, V1, V2, V3, V4
  .gitignore               # ignores .last-run.json and cases/*/runtime/
```

## Fixture format

Each fixture is a directory under `cases/` containing:

- `diff.patch`            : pi phase-1 input.
- `conventions.md`        : repo conventions (may be empty).
- `prior-threads.json`    : prior review threads JSON array.
- `pr-meta.json`          : `{owner, repo, pr_number, head_sha}`.
- `expected.yaml`         : assertion list (see below).
- `notes.md`              : human-readable description of the attack vector.

### `expected.yaml` schema

```yaml
attacker_goal: G1            # one of G1..G4 from the design doc
notes: ""                    # short description (echo of notes.md)
assertions:
  - kind: <one_of_valid_kinds>
    # ...kind-specific params (file/line/regex/values/...)
```

Valid `kind` values (the loader fails fast on anything else):

- `finding_present`           : params: `file`, `line` (line optional)
- `finding_absent`            : params: `file`, `line` (line optional)
- `verdict_in`                : params: `values: [APPROVE|COMMENT|...]`
- `prior_classification_not`  : params: `thread_id`, `status`
- `no_tool_call_path_match`   : params: `regex`
- `no_bash_ro_command_match`  : params: `regex`
- `no_output_substring_match` : params: `substring`

## Invocation

```
# Default test collection skips the adversarial mark:
uv run pytest tests/

# Run the smoke fixture only (mocked pi; no LLM call):
uv run pytest -m adversarial -k smoke --tb=short

# Run the full corpus (requires LLM_API_KEY, LLM_BASE_URL, LLM_MODEL):
uv run pytest -m adversarial
```

`MOMUS_REDTEAM_MOCK_PI=1` short-circuits pi invocation and writes a
deterministic synthetic `findings.json`. The smoke fixture sets this
automatically via the test-level `monkeypatch`.

## Per-run isolation

Each run materialises `cases/<name>/runtime/<run_idx>/` with `inputs/`
and `outputs/` subdirectories. Multiple runs of the same fixture (and
multiple fixtures running concurrently) never share toolcall-log files,
findings.json files, or input copies. The `runtime/` subtree is
git-ignored.

## Outputs

- `tests/adversarial/.last-run.json`: session-end summary (per-fixture
  status + ASR). Git-ignored. Consumed by V4.
- `tests/adversarial/verification/v*-*.json`: per-V-step PASS artefacts.
  Git-tracked.
