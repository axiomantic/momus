"""Configuration loading + per-repo override merging."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULTS_PATH = Path(__file__).resolve().parent / "config-defaults.yaml"
ALLOWED_SEVERITIES = {"critical", "high", "medium", "low", "nit"}
ALLOWED_RUN_ID_SCHEMES = {"alpha", "numeric", "off"}
ALLOWED_FIRST_REVIEW_POLICIES = {"never", "if_no_findings", "if_no_blocking"}


@dataclass
class ReviewConfig:
    blocking_severities: list[str]
    require_calibration: bool
    emit_nits: bool
    max_findings: int
    noteworthy_max: int
    run_id_scheme: str
    repo_emphasis: str


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
    skip_post_on_empty: bool


@dataclass
class Config:
    review: ReviewConfig
    conventions: ConventionsConfig
    post: PostConfig
    verify: VerifyConfig


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

    blocking = list(review.get("blocking_severities", []))
    bad = [s for s in blocking if s not in ALLOWED_SEVERITIES]
    if bad:
        raise ValueError(f"review.blocking_severities: unknown severities {bad}")

    scheme = review.get("run_id_scheme")
    if scheme not in ALLOWED_RUN_ID_SCHEMES:
        raise ValueError(f"review.run_id_scheme: must be one of {ALLOWED_RUN_ID_SCHEMES}, got {scheme!r}")

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
            skip_post_on_empty=bool(verify.get("skip_post_on_empty", True)),
        ),
    )
