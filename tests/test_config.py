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
