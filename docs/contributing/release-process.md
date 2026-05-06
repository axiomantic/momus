# Release process

The release flow is small and largely automated. A merge to `main`
that bumps `version` in `pyproject.toml` triggers a tag, a GitHub
Release, and versioned docs. This page documents the moving parts
so the maintainer can verify each step.

## The flow at a glance

1. Open a PR with the version bump and CHANGELOG entry.
2. Merge to `main`.
3. `release.yml` detects the version change, creates the immutable
   `vX.Y.Z` tag, fast-forwards the floating `vX` major-alias tag,
   and publishes a GitHub Release with auto-generated notes.
4. `docs.yml` runs in parallel: the push to `main` re-publishes the
   `main` and `latest` mike aliases; the new tag publishes the
   immutable `vX.Y.Z` docs version and updates the floating `vX`
   docs alias.

There is no manual `git tag`. Bumping the version in
`pyproject.toml` is the action that releases.

## Step 1: prepare the bump

Edit two files on a release branch:

- `pyproject.toml`: bump `version = "X.Y.Z"` under `[project]`.
- `CHANGELOG.md`: add a section header `## [X.Y.Z] - YYYY-MM-DD` and
  fill in the `### Added` / `### Changed` / `### Fixed` / `### Security`
  sections. The existing entries (1.0.0, 1.1.0) are the format.

Versioning follows [SemVer](https://semver.org/). Patch for bug
fixes, minor for new features that do not break compatibility, major
for breaking changes. The release workflow rejects a non-greater
version (`release.yml` lines 62-68) and a version that does not
match `MAJOR.MINOR.PATCH` (lines 57-60).

Open the PR, get it reviewed (momus reviews itself; see the
self-dogfood workflow), and merge.

## Step 2: what `release.yml` does

The release workflow (`.github/workflows/release.yml`) triggers on
push to `main` when `pyproject.toml` is touched. It:

1. Parses the new version from `pyproject.toml`.
2. Compares against the previous version on the parent commit. If
   unchanged, it exits cleanly (no release).
3. Validates the new version is `MAJOR.MINOR.PATCH` and strictly
   greater than the previous.
4. Creates the immutable annotated tag `vX.Y.Z` (refuses to overwrite
   if it already exists).
5. Force-updates the floating `vX` major alias to point at the new
   tag.
6. Pushes both tags.
7. Creates a GitHub Release with `gh release create
   --generate-notes`.

The tag pattern is `vX.Y.Z` for immutable releases and `vX` for the
floating major-version alias. Existing tags: `v1`, `v1.0.0` (run
`git tag` to confirm).

## Step 3: what `docs.yml` does

The docs workflow (`.github/workflows/docs.yml`) publishes via
[mike](https://github.com/jimporter/mike), the mkdocs plugin for
versioned site deployment. The site lives on the `gh-pages` branch
and serves from `https://elijahr.github.io/momus/`.

mike maintains named **versions** and **aliases** that map to those
versions. The site root redirects to the default version.

`docs.yml` triggers on:

- **Push to `main`** that touches `docs/**`, `mkdocs.yml`,
  `momus/**`, `pyproject.toml`, or the workflow itself. Publishes
  the `main` version and re-points the `latest` alias at the new
  build:

  ```
  uv run mike deploy --push --update-aliases main latest
  uv run mike set-default --push latest
  ```

  This is the change in commit
  [`a5031f6`](https://github.com/elijahr/momus/commit/a5031f6):
  `latest` now tracks the main branch tip rather than the most
  recent tag. Users hitting `https://elijahr.github.io/momus/` get
  the freshest docs, not the last-released ones.

- **Tag push matching `v*.*.*`** (created by `release.yml`).
  Publishes the immutable `vX.Y.Z` version and updates the floating
  `vX` major-version alias:

  ```
  uv run mike deploy --push --update-aliases "$VERSION" "$MAJOR"
  ```

  Tag publishes intentionally do NOT touch `latest`. `latest` always
  tracks `main`, so a tag publish leaves the public root pointing at
  main's tip. Users who want pinned docs visit
  `/v1.2.3/` or `/v1/` directly.

The build runs `mkdocs build --strict` first as a sanity check; any
broken cross-link or missing nav entry fails the workflow before
publish.

## Step 4: verification

After both workflows complete:

- `git tag` shows the new `vX.Y.Z` and the updated `vX`.
- `gh release view vX.Y.Z` shows the release page with notes.
- `https://elijahr.github.io/momus/` redirects to `/latest/` and
  shows the new content.
- `https://elijahr.github.io/momus/vX.Y.Z/` serves the immutable
  release docs.

If the docs build fails (`--strict` rejects a broken anchor or
missing nav entry), the release tag exists but the docs do not
update. Fix the docs in a follow-up PR; the next push to `main` will
retry the publish.

## Common questions

**Can I retag?** No. `release.yml` refuses to overwrite an immutable
`vX.Y.Z` tag. If you cut a bad release, bump to the next patch and
release again. The mike deploy is idempotent; rerunning the same
version is a no-op.

**What if the version bump and a feature land in the same PR?** Fine.
The release workflow keys off the version diff against the parent
commit, not the file count. Just keep the CHANGELOG entry honest.

**How do I publish a pre-release?** The current workflow does not
support pre-release tags (`v1.2.3-rc.1` would fail the strict
`MAJOR.MINOR.PATCH` regex at `release.yml:57`). If you need this,
extend the regex in the workflow and the docs publish step in
parallel.
