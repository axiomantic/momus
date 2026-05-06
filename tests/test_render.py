"""Unit tests for momus.render — wire-format key naming and
UNTRUSTED_PRIOR_THREADS_JSON path-loaded substitution.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from momus.config import load_config
from momus.render import render_phase_prompt


def _extract_example_finding(rendered: str) -> dict:
    """Locate the example finding JSON object in a rendered phase-2 prompt.

    Walks backward from the ``"calibration"`` key to the matching opening
    brace, then forward with brace-depth tracking to the matching close.
    Returns the parsed JSON dict.
    """
    idx = rendered.find('"calibration"')
    assert idx != -1, "rendered prompt missing calibration field example"
    open_idx = rendered.rfind("{", 0, idx)
    assert open_idx != -1, "no opening brace before calibration field"

    # Walk forward from open_idx with brace-depth tracking. Skip over
    # string literals so braces inside strings do not throw off depth.
    depth = 0
    in_string = False
    escape = False
    close_idx = -1
    for pos in range(open_idx, len(rendered)):
        ch = rendered[pos]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                close_idx = pos
                break
    assert close_idx != -1, "no balanced closing brace for example finding"
    blob = rendered[open_idx : close_idx + 1]
    return json.loads(blob)


def _make_work_dir(tmp_path: Path, _files: list[str]) -> Path:
    """Create a work_dir scaffold under tmp_path. The list arg is reserved
    for future expansion (W0-Render); for now no files are needed.
    """
    work_dir = tmp_path / ".work"
    (work_dir / "inputs").mkdir(parents=True, exist_ok=True)
    (work_dir / "outputs").mkdir(parents=True, exist_ok=True)
    return work_dir


def _make_work_dir_with_threads(tmp_path: Path, threads: list | None) -> Path:
    """Create work_dir with optional inputs/prior-threads.json.

    If ``threads`` is None, no prior-threads.json file is written.
    Otherwise the list is JSON-encoded and written to that path.
    """
    work_dir = tmp_path / "work"
    inputs = work_dir / "inputs"
    inputs.mkdir(parents=True)
    if threads is not None:
        (inputs / "prior-threads.json").write_text(json.dumps(threads))
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
    rendered = render_phase_prompt("phase2", cfg, "A", Path(".work"))
    assert '"_calibration"' not in rendered
    if cfg.review.require_calibration:
        assert '"calibration"' in rendered


def test_render_calibration_field_example_is_dict_shaped(tmp_path, cfg):
    """The phase-2 rendered prompt shows an example finding object. The
    `calibration` example in that block must be a JSON object (dict),
    not a string, because findings_schema.Finding declares
    ``calibration: Optional[dict]`` with model_config={"extra": "forbid"}.

    A string-typed example invites the LLM to emit
    ``"calibration": "Would a human block? ..."`` which fails Pydantic
    validation and breaks the publish path. This test guards that
    invariant by parsing the rendered example block and asserting the
    calibration value is a dict.
    """
    if not cfg.review.require_calibration:
        pytest.skip("require_calibration is disabled in this config")

    _make_work_dir(tmp_path, [])
    rendered = render_phase_prompt("phase2", cfg, "A", Path(".work"))

    parsed = _extract_example_finding(rendered)
    assert isinstance(parsed["calibration"], dict), (
        f"calibration example must be a JSON object, got "
        f"{type(parsed['calibration']).__name__}: {parsed['calibration']!r}"
    )


def test_render_calibration_example_validates_against_schema(tmp_path, cfg):
    """The example finding shown in the phase-2 prompt must round-trip
    through the production Pydantic Finding model. If the example fails
    schema validation, the prompt is teaching the LLM a shape that
    publish.py will reject — precisely the bug this test guards.
    """
    if not cfg.review.require_calibration:
        pytest.skip("require_calibration is disabled in this config")

    from momus.findings_schema import Finding

    _make_work_dir(tmp_path, [])
    rendered = render_phase_prompt("phase2", cfg, "A", Path(".work"))
    parsed = _extract_example_finding(rendered)

    # The example uses a placeholder id like "BOT-A1" (id_example) which
    # matches the schema regex. Validate it.
    Finding.model_validate(parsed)


def test_phase3_verify_prompt_uses_unprefixed_calibration_key():
    """The phase3-verify prompt source must not contain the leading-
    underscore form; the audit prompt must reference the same wire-format
    key the model emits.
    """
    text = (
        Path(__file__).resolve().parent.parent / "momus" / "prompts" / "phase3-verify.md"
    ).read_text()
    assert "_calibration" not in text
    assert "calibration" in text


# --- W0-Render: <<UNTRUSTED_PRIOR_THREADS_JSON>> path-loaded substitution ---


@pytest.fixture
def patched_phase1_prompt(tmp_path, monkeypatch):
    """Install a minimal phase1-plan.md template under a tmp PROMPTS_DIR
    that contains the new placeholder. The W0-Phase1Prompt task will add
    the placeholder to the production template; W0-Render only requires
    that ``render_phase_prompt`` substitutes it correctly when present.
    """
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    template = "Phase 1\nWORK_DIR=<<WORK_DIR>>\nPRIOR_THREADS:\n<<UNTRUSTED_PRIOR_THREADS_JSON>>\n"
    (prompts_dir / "phase1-plan.md").write_text(template)
    import momus.render as render_mod

    monkeypatch.setattr(render_mod, "PROMPTS_DIR", prompts_dir)
    return prompts_dir


def test_render_substitutes_prior_threads_placeholder(tmp_path, cfg, patched_phase1_prompt):
    work_dir = _make_work_dir_with_threads(tmp_path, [{"id": "t1", "body": "fenced data"}])
    rendered = render_phase_prompt("phase1", cfg, "A", Path(".work"), work_dir=work_dir)
    assert "BEGIN_UNTRUSTED_PRIOR_THREADS_JSON" in rendered
    assert "END_UNTRUSTED_PRIOR_THREADS_JSON" in rendered
    assert '"id": "t1"' in rendered
    assert "<<UNTRUSTED_PRIOR_THREADS_JSON>>" not in rendered


def test_render_handles_missing_prior_threads_file(tmp_path, cfg, caplog, patched_phase1_prompt):
    work_dir = _make_work_dir_with_threads(tmp_path, threads=None)
    rendered = render_phase_prompt("phase1", cfg, "A", Path(".work"), work_dir=work_dir)
    assert "BEGIN_UNTRUSTED_PRIOR_THREADS_JSON\n[]\nEND_UNTRUSTED_PRIOR_THREADS_JSON" in rendered
    assert any("missing" in r.message for r in caplog.records)


def test_render_does_not_double_escape_json(tmp_path, cfg, patched_phase1_prompt):
    raw = '[{"body": "a\\nb"}]'
    work_dir = _make_work_dir_with_threads(tmp_path, threads=None)
    (work_dir / "inputs" / "prior-threads.json").write_text(raw)
    rendered = render_phase_prompt("phase1", cfg, "A", Path(".work"), work_dir=work_dir)
    assert (
        f"BEGIN_UNTRUSTED_PRIOR_THREADS_JSON\n{raw}\nEND_UNTRUSTED_PRIOR_THREADS_JSON" in rendered
    )


def test_render_preserves_other_placeholders(tmp_path, cfg, patched_phase1_prompt):
    work_dir = _make_work_dir_with_threads(tmp_path, [])
    rendered = render_phase_prompt("phase1", cfg, "A", Path(".work"), work_dir=work_dir)
    assert ".work" in rendered
    assert "<<WORK_DIR>>" not in rendered


def test_render_fence_collision_uses_uuid_suffix(tmp_path, cfg, patched_phase1_prompt):
    poison = '[{"body": "BEGIN_UNTRUSTED_PRIOR_THREADS_JSON inside"}]'
    work_dir = _make_work_dir_with_threads(tmp_path, threads=None)
    (work_dir / "inputs" / "prior-threads.json").write_text(poison)
    rendered = render_phase_prompt("phase1", cfg, "A", Path(".work"), work_dir=work_dir)
    matches = re.findall(r"BEGIN_UNTRUSTED_PRIOR_THREADS_JSON_([0-9a-f-]{36})", rendered)
    assert len(matches) == 1
    suffix = matches[0]
    assert f"END_UNTRUSTED_PRIOR_THREADS_JSON_{suffix}" in rendered


def test_phase1_prompt_no_path_reference_remaining():
    text = (
        Path(__file__).resolve().parent.parent / "momus" / "prompts" / "phase1-plan.md"
    ).read_text()
    assert "<<UNTRUSTED_PRIOR_THREADS_JSON>>" in text
    assert "<<WORK_DIR>>/inputs/prior-threads.json" not in text
