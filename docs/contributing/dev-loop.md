# Dev loop

Getting set up locally and running the test suite. The toolchain is
small and the commands are exactly the ones CI runs; no ceremony.

## One-time setup

```
git clone https://github.com/elijahr/momus.git
cd momus
uv sync --group dev
npm ci
```

`uv sync --group dev` installs the runtime dependencies plus the dev
group (`pytest`, `pytest-tripwire`, `ruff`, `mypy`, `pre-commit`).
`npm ci` installs the pinned pi runtime
(`@mariozechner/pi-coding-agent@0.72.1`) from `package-lock.json`,
plus the `typescript` and `@types/bun` devDependencies the TypeScript
type check needs.
Use `npm ci`, not `npm install`: the pin is intentional and the unit
checks in `tests/integration/test_pi_tool_enforcement.py` will fail if
it drifts.

## The toolchain

Three tools cover lint, types, and tests. CI (`.github/workflows/ci.yml`)
runs them in this order; mirror it locally before opening a PR.

```
uv run ruff check .
uv run ruff format --check .
uv run mypy momus tests
uv run pytest -m 'not adversarial'
```

The pytest default already excludes the adversarial mark
(`addopts = -m 'not adversarial' -ra` in `pyproject.toml`), so the bare
`uv run pytest` form does the same thing as the explicit
`-m 'not adversarial'`. Keeping the mark in the command makes it
obvious you are running the fast suite.

To autofix what ruff can:

```
uv run ruff check --fix .
uv run ruff format .
```

## Running a subset

Targeted test runs are usually what you want. Match scope to change.

- A single file: `uv run pytest tests/test_render.py`
- A single test: `uv run pytest tests/test_render.py::test_renders_phase2`
- A keyword filter: `uv run pytest -k publish`
- Just unit tests (the default): `uv run pytest -m unit` or just
  `uv run pytest`
- Integration (spawns pi, slower, needs `LLM_API_KEY`):
  `uv run pytest -m integration`
- Adversarial corpus (full LLM runs; opt-in):
  `uv run pytest -m adversarial`

Test marks are declared in `pyproject.toml` under
`[tool.pytest.ini_options].markers`.

`-ra` in the same `addopts` prints a short summary line for every
non-passing outcome. A skipped test is otherwise a single `s` in the
progress line and a zero exit, which reads identically to a test that
ran. The integration test above skips whenever `LLM_API_KEY` is unset,
including on every CI run, so the reason is now printed rather than
inferred.

## TypeScript extension tests

The pi extension (`momus/extensions/readonly-tools.ts`) has its own
test file. Run it with bun:

```
npx --no-install tsc --noEmit
bun test momus/extensions/readonly-tools.test.ts
```

`bun test` covers cwd containment, realpath checks, and the `bash_ro`
argv allowlist. It does not check types: bun strips them, so the suite
is green on code `tsc` rejects. That is what the `tsc --noEmit` line is
for, and CI runs both. Settings live in `tsconfig.json` (`strict`, and
only `momus/extensions/**/*.ts` in scope). If you change
`readonly-tools.ts`, run both before pushing.

## Pre-commit

`pre-commit` is a dev dep but not auto-installed. If you want the
hooks active:

```
uv run pre-commit install
```

CI does not depend on pre-commit running locally; it re-runs ruff
and mypy on its own. The hook is purely a "catch it before you push"
convenience.

## Where to read next

- `adversarial-corpus.md` if your change touches review behavior or
  injection containment.
- `modifying-prompts.md` if your change edits anything under
  `momus/prompts/`.
- `release-process.md` if you are cutting a release.
