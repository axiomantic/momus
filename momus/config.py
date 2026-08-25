"""Configuration loading + per-repo override merging."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]  # types-PyYAML stubs not in dev deps

from .diff_filter import unsupported_pattern

DEFAULTS_PATH = Path(__file__).resolve().parent / "config-defaults.yaml"
ALLOWED_SEVERITIES = {"critical", "high", "medium", "low", "nit"}
ALLOWED_RUN_ID_SCHEMES = {"alpha", "numeric", "off"}
ALLOWED_FIRST_REVIEW_POLICIES = {"never", "if_no_findings", "if_no_blocking"}
# Names of emphasis-module files shipped under momus/prompts/emphasis/.
# When a repo opts in via ``review.emphasis_modules``, the renderer
# inlines each module's body into ``<<REPO_EMPHASIS>>`` ahead of the
# free-form ``review.repo_emphasis`` string.
ALLOWED_EMPHASIS_MODULES = {"security", "dead_code", "quality_checklist", "test_quality"}


@dataclass
class ReviewConfig:
    blocking_severities: list[str]
    require_calibration: bool
    emit_nits: bool
    max_findings: int
    noteworthy_max: int
    run_id_scheme: str
    repo_emphasis: str
    # Default empty list keeps existing test fixtures and downstream
    # ReviewConfig constructions backward-compatible. New module library
    # is opt-in via .momus.yaml.
    emphasis_modules: list[str] = field(default_factory=list)


@dataclass
class ConventionsConfig:
    files: list[str]
    globs: list[str]


@dataclass
class PostConfig:
    first_review_approve_policy: str
    allow_human_approve_override: bool


@dataclass
class VerifyConfig:
    enabled: bool


@dataclass
class ProviderConfig:
    """
    LLM provider override at the per-repo level. Empty strings mean
    "use whatever the workflow set in LLM_MODEL / LLM_BASE_URL". Set
    explicitly here when a particular repo needs a different model
    or endpoint than the workflow's default (e.g. one repo uses
    Claude direct while the rest use OpenRouter).
    """

    model: str
    base_url: str


@dataclass
class ScopeConfig:
    """
    Which changed files the review looks at.

    ``exclude_paths`` is a list of gitignore-syntax patterns. Setting the
    key in ``.momus.yaml`` REPLACES the shipped defaults rather than
    extending them, so a repo that wants the defaults plus one more entry
    restates the whole list.
    """

    exclude_paths: list[str]
    exclude_binary_files: bool


@dataclass
class ChecksConfig:
    """
    Optional Check Run posting alongside the Review object. When enabled,
    Momus posts a check that surfaces on the PR header (and can be made a
    required check via branch protection). Requires the bot's token to
    have ``Checks: Write`` — set on the GitHub App, or available by
    default on ``GITHUB_TOKEN`` inside Actions.
    """

    enabled: bool
    name: str


@dataclass
class Config:
    review: ReviewConfig
    conventions: ConventionsConfig
    post: PostConfig
    verify: VerifyConfig
    checks: ChecksConfig
    provider: ProviderConfig
    # Default keeps hand-built Config objects (test fixtures, downstream
    # callers) working and inert: no patterns means nothing is excluded.
    # load_config always supplies the shipped defaults.
    scope: ScopeConfig = field(
        default_factory=lambda: ScopeConfig(exclude_paths=[], exclude_binary_files=False)
    )


def load_config(repo_root: Path) -> Config:
    """Load defaults; merge `.momus.yaml` from repo_root if present."""
    defaults = _read_yaml(DEFAULTS_PATH)
    override_path = repo_root / ".momus.yaml"
    if override_path.exists():
        override = _read_yaml(override_path)
        merged = _deep_merge(defaults, override)
    else:
        merged = defaults
    return _to_config(merged)


def _read_yaml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected mapping at top level")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _to_config(data: dict[str, Any]) -> Config:
    review = data.get("review", {})
    conventions = data.get("conventions", {})
    post = data.get("post", {})
    verify = data.get("verify", {})
    checks = data.get("checks", {})
    provider = data.get("provider", {})
    scope = data.get("scope", {})

    blocking = list(review.get("blocking_severities", []))
    bad = [s for s in blocking if s not in ALLOWED_SEVERITIES]
    if bad:
        raise ValueError(f"review.blocking_severities: unknown severities {bad}")

    scheme = review.get("run_id_scheme")
    if scheme not in ALLOWED_RUN_ID_SCHEMES:
        raise ValueError(
            f"review.run_id_scheme: must be one of {ALLOWED_RUN_ID_SCHEMES}, got {scheme!r}"
        )

    raw_emphasis_modules = review.get("emphasis_modules", [])
    if raw_emphasis_modules is None:
        raw_emphasis_modules = []
    if not isinstance(raw_emphasis_modules, list):
        raise ValueError(
            f"review.emphasis_modules: must be a list, got {type(raw_emphasis_modules).__name__}"
        )
    emphasis_modules = list(raw_emphasis_modules)
    bad_modules = [m for m in emphasis_modules if m not in ALLOWED_EMPHASIS_MODULES]
    if bad_modules:
        raise ValueError(
            f"review.emphasis_modules: unknown module(s) {bad_modules}. "
            f"Allowed: {sorted(ALLOWED_EMPHASIS_MODULES)}"
        )

    exclude_paths = _read_exclude_paths(scope)

    policy = post.get("first_review_approve_policy")
    if policy not in ALLOWED_FIRST_REVIEW_POLICIES:
        raise ValueError(
            f"post.first_review_approve_policy: must be one of "
            f"{ALLOWED_FIRST_REVIEW_POLICIES}, got {policy!r}"
        )

    return Config(
        review=ReviewConfig(
            blocking_severities=blocking,
            require_calibration=bool(review.get("require_calibration", True)),
            emit_nits=bool(review.get("emit_nits", True)),
            max_findings=int(review.get("max_findings", 50)),
            noteworthy_max=int(review.get("noteworthy_max", 3)),
            run_id_scheme=scheme,
            repo_emphasis=str(review.get("repo_emphasis", "")),
            emphasis_modules=emphasis_modules,
        ),
        conventions=ConventionsConfig(
            files=list(conventions.get("files", [])),
            globs=list(conventions.get("globs", [])),
        ),
        post=PostConfig(
            first_review_approve_policy=policy,
            allow_human_approve_override=bool(post.get("allow_human_approve_override", False)),
        ),
        verify=VerifyConfig(
            enabled=bool(verify.get("enabled", True)),
        ),
        checks=ChecksConfig(
            enabled=bool(checks.get("enabled", False)),
            name=str(checks.get("name", "Momus Code Review")),
        ),
        provider=ProviderConfig(
            model=str(provider.get("model", "")),
            base_url=str(provider.get("base_url", "")),
        ),
        scope=ScopeConfig(
            exclude_paths=exclude_paths,
            exclude_binary_files=bool(scope.get("exclude_binary_files", False)),
        ),
    )


def _read_exclude_paths(scope: dict[str, Any]) -> list[str]:
    raw = scope.get("exclude_paths", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError(f"scope.exclude_paths: must be a list, got {type(raw).__name__}")
    patterns = [str(p) for p in raw]
    rejected = [p for p in patterns if unsupported_pattern(p)]
    if rejected:
        raise ValueError(
            f"scope.exclude_paths: unsupported pattern(s) {rejected}. Negated "
            "character classes ([!abc], [^abc]) are read differently by the two "
            "matchers momus uses (Python pathspec for the diff, the npm `ignore` "
            "package for the tool layer), so a file could leave the diff while "
            "staying readable. Rewrite the pattern without one."
        )
    return patterns
