"""Unit tests for momus.render — wire-format key naming."""

from __future__ import annotations

from pathlib import Path

import pytest

from momus.config import load_config
from momus.render import render_phase_prompt


def _make_work_dir(tmp_path: Path, _files: list[str]) -> Path:
    """Create a work_dir scaffold under tmp_path. The list arg is reserved
    for future expansion (W0-Render); for now no files are needed.
    """
    work_dir = tmp_path / ".work"
    (work_dir / "inputs").mkdir(parents=True, exist_ok=True)
    (work_dir / "outputs").mkdir(parents=True, exist_ok=True)
    return work_dir


@pytest.fixture
def cfg(tmp_path: Path):
    """Load default config rooted at tmp_path. Defaults have
    require_calibration=True per momus/config-defaults.yaml.
    """
    return load_config(tmp_path)


def test_render_calibration_field_uses_unprefixed_key(tmp_path, cfg):
    """The rendered phase-2 prompt must use the unprefixed JSON key
    `"calibration"` (not `"_calibration"`) so Pydantic v2 with
    extra='forbid' accepts the field.

    TODO(W0-Render): switch to kwarg form once render_phase_prompt
    accepts work_dir as a kwarg.
    """
    _make_work_dir(tmp_path, [])
    rendered = render_phase_prompt(
        "phase2", cfg, "A", Path(".work")
    )
    assert '"_calibration"' not in rendered
    if cfg.review.require_calibration:
        assert '"calibration"' in rendered


def test_phase3_verify_prompt_uses_unprefixed_calibration_key():
    """The phase3-verify prompt source must not contain the leading-
    underscore form; the audit prompt must reference the same wire-format
    key the model emits.
    """
    text = (Path(__file__).resolve().parent.parent /
            "momus" / "prompts" / "phase3-verify.md").read_text()
    assert "_calibration" not in text
    assert "calibration" in text
