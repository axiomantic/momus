"""Unit tests for config loading and overrides."""

from __future__ import annotations

from pathlib import Path

import pytest
from momus.config import load_config


def _write_yaml(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_provider_defaults_to_empty_strings(tmp_path: Path) -> None:
    """Empty provider config means 'use whatever the workflow env set'."""
    cfg = load_config(tmp_path)
    assert cfg.provider.model == ""
    assert cfg.provider.base_url == ""


def test_provider_override_from_repo_yaml(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / ".momus.yaml",
        """
provider:
  model: anthropic/claude-sonnet-4-6
  base_url: https://api.anthropic.com/v1
""",
    )
    cfg = load_config(tmp_path)
    assert cfg.provider.model == "anthropic/claude-sonnet-4-6"
    assert cfg.provider.base_url == "https://api.anthropic.com/v1"


def test_provider_partial_override_only_changes_specified_fields(
    tmp_path: Path,
) -> None:
    _write_yaml(
        tmp_path / ".momus.yaml",
        """
provider:
  model: openai/gpt-4o
""",
    )
    cfg = load_config(tmp_path)
    assert cfg.provider.model == "openai/gpt-4o"
    # base_url unset -> remains default empty string (workflow env wins).
    assert cfg.provider.base_url == ""


def test_checks_default_disabled_with_canonical_name(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    assert cfg.checks.enabled is False
    assert cfg.checks.name == "Momus Code Review"


def test_checks_override(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / ".momus.yaml",
        """
checks:
  enabled: true
  name: Acme Review
""",
    )
    cfg = load_config(tmp_path)
    assert cfg.checks.enabled is True
    assert cfg.checks.name == "Acme Review"


def test_emphasis_modules_default_empty(tmp_path: Path) -> None:
    """Default config must expose ``emphasis_modules`` as an empty list so
    that pre-existing repos with no opt-in continue to render the prompt
    using only the free-form ``repo_emphasis`` string.
    """
    cfg = load_config(tmp_path)
    assert cfg.review.emphasis_modules == []


def test_emphasis_modules_load_known_names(tmp_path: Path) -> None:
    """Known module names must load as a list preserving order. The four
    valid module names are ``security``, ``dead_code``,
    ``quality_checklist``, and ``test_quality``.
    """
    _write_yaml(
        tmp_path / ".momus.yaml",
        """
review:
  emphasis_modules: [security, dead_code]
""",
    )
    cfg = load_config(tmp_path)
    assert cfg.review.emphasis_modules == ["security", "dead_code"]


def test_emphasis_modules_unknown_name_raises(tmp_path: Path) -> None:
    """Unknown module names must raise a clear ``ValueError`` naming the
    offending entry. Validation happens at config load time so that typos
    fail loudly on startup rather than silently rendering a degraded
    prompt.
    """
    _write_yaml(
        tmp_path / ".momus.yaml",
        """
review:
  emphasis_modules: [bogus_name]
""",
    )
    with pytest.raises(ValueError, match="bogus_name"):
        load_config(tmp_path)


def test_emphasis_modules_all_four_known(tmp_path: Path) -> None:
    """All four documented module names must validate as a complete set."""
    _write_yaml(
        tmp_path / ".momus.yaml",
        """
review:
  emphasis_modules: [security, dead_code, quality_checklist, test_quality]
""",
    )
    cfg = load_config(tmp_path)
    assert cfg.review.emphasis_modules == [
        "security",
        "dead_code",
        "quality_checklist",
        "test_quality",
    ]
