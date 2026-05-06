# Contributing to momus

Thanks for the interest. momus is a small project with a tight scope;
the contributor docs below cover everything you need.

## Quick start

```
git clone https://github.com/elijahr/momus.git
cd momus
uv sync --group dev
npm ci
uv run pytest -m 'not adversarial'
```

`npm ci` (not `npm install`) installs the pinned pi runtime from
`package-lock.json`.

## Found a bug? Have a feature request?

Open an issue at
[github.com/elijahr/momus/issues](https://github.com/elijahr/momus/issues).
Include a minimal reproduction for bugs (a tiny `.momus.yaml` and the
diff you ran against, ideally). For feature requests, name the
problem you hit before the solution you have in mind.

## Working on a change?

- [Adversarial corpus](docs/contributing/adversarial-corpus.md) —
  required reading if your change touches review behavior or
  injection containment.
- [Modifying prompts](docs/contributing/modifying-prompts.md) —
  required reading if your change touches anything under
  `momus/prompts/`.
- [Dev loop](docs/contributing/dev-loop.md) — toolchain commands.
- [Release process](docs/contributing/release-process.md) — for
  maintainers cutting a release.

## Code style

`ruff` and `mypy` enforce style and types in CI. Run
`uv run ruff check .`, `uv run ruff format --check .`, and
`uv run mypy momus tests` locally before opening a PR. Project
conventions (no em-dashes in prose, no AI-attribution footers,
atomic commits) live in [`AGENTS.md`](AGENTS.md).

## Pull requests

- Keep PRs small and focused. One concern per PR.
- Behavior changes need tests. Bug fixes need a test that fails
  before the fix and passes after.
- User-visible changes need a `CHANGELOG.md` entry under the
  appropriate `### Added` / `### Changed` / `### Fixed` /
  `### Security` heading.
- The bot reviews its own PRs (see `.github/workflows/momus.yml`).
  Treat its findings as you would any reviewer's: address the real
  ones, push back on the wrong ones in-thread.
