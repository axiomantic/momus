"""Pydantic v2 schema for the phase-2 findings.json output (W5-PydanticSchema).

The schema mirrors the contract in `momus/prompts/phase2-review.md`. It is
the strict gate before `momus.publish.publish` posts to GitHub: any LLM
output that does not match this shape (extra keys, wrong types, oversize
text) fails closed and the run exits nonzero with no PR comment posted.

Rationale for `extra='forbid'` everywhere
-----------------------------------------
An attacker-controlled diff or prior thread that nudges the model into
emitting `{"shell_command": "rm -rf /"}` inside a finding should not pass
silently. Forbidding extras turns prompt-injection-induced shape drift
into a fail-closed validation error, which the publisher logs and
refuses to ship.

Field-name notes
----------------
- `calibration` (no underscore prefix). Pydantic v2 reserves
  underscore-prefixed names for private attributes; the prompt-render
  layer was renamed (commit `2c9ccdb`) so the wire-format key is
  `calibration`. See D4 in the implementation plan and §W5 of the design.
- `noteworthy` is conditionally rendered per `render.py`'s
  `<<NOTEWORTHY_FIELD>>` placeholder; kept `Optional[list[str]]` so docs
  with the placeholder collapsed to empty still validate.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

Severity = Literal["critical", "high", "medium", "low", "nit"]
Verdict = Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]
PriorStatusValue = Literal["fixed", "unfixed", "partially_fixed", "removed"]
Side = Literal["LEFT", "RIGHT"]


class Finding(BaseModel):
    """A single review finding. Mirrors phase2-review.md's Output section."""

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    file: str = Field(min_length=1, max_length=512)
    line: int = Field(ge=1)
    end_line: Optional[int] = Field(default=None, ge=1)
    side: Side = "RIGHT"
    severity: Severity
    category: str = Field(min_length=1, max_length=64)
    blocking: bool = False
    title: str = Field(min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=4000)
    suggestion: Optional[str] = Field(default=None, max_length=8000)
    # Rendered by phase 2 only when require_calibration is set; see
    # render.py:112-127. Free-form dict per design §W5 (the prompt asks
    # for a one-line "would a human block?" justification, so the keys
    # are not pinned).
    calibration: Optional[dict] = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check_end_line_ge_line(self) -> "Finding":
        """Cross-field rule: end_line, when present, must be >= line.

        Pydantic field-level constraints can pin both to ge=1 but cannot
        express the inter-field relationship. A finding with end_line < line
        is always a malformed range (publish.py would emit it as
        start_line > line, which GitHub rejects with 422).
        """
        if self.end_line is not None and self.end_line < self.line:
            raise ValueError(
                f"end_line ({self.end_line}) must be >= line ({self.line})"
            )
        return self


class PriorStatusEntry(BaseModel):
    """A prior-finding status update; references an id from the prior run."""

    id: str = Field(min_length=1, max_length=64)
    status: PriorStatusValue

    model_config = {"extra": "forbid"}


class FindingsDoc(BaseModel):
    """Top-level findings.json shape produced by phase 2."""

    summary: str = Field(min_length=1, max_length=2000)
    verdict: Verdict
    tally: dict[str, int] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list, max_length=200)
    prior_findings_status: list[PriorStatusEntry] = Field(default_factory=list)
    # Rendered conditionally per <<NOTEWORTHY_FIELD>> in render.py.
    noteworthy: Optional[list[str]] = None

    model_config = {"extra": "forbid"}
