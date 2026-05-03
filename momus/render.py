"""Render phase prompts by substituting <<PLACEHOLDER>> tokens."""

from __future__ import annotations

from pathlib import Path

from .config import Config

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def render_phase_prompt(phase: str, config: Config, run_id: str) -> str:
    """Render the phase prompt for ``phase`` (one of: phase1, phase2, phase3)."""
    template = (PROMPTS_DIR / f"{phase}-{_phase_suffix(phase)}.md").read_text(encoding="utf-8")
    substitutions = _substitutions(config, run_id)
    rendered = template
    for key, value in substitutions.items():
        rendered = rendered.replace(f"<<{key}>>", value)
    leftover = _find_unsubstituted(rendered)
    if leftover:
        raise ValueError(
            f"{phase}: unsubstituted placeholders {leftover}. "
            f"Add them to render._substitutions or remove from the prompt."
        )
    return rendered


def _phase_suffix(phase: str) -> str:
    return {"phase1": "plan", "phase2": "review", "phase3": "verify"}[phase]


def _substitutions(config: Config, run_id: str) -> dict[str, str]:
    review = config.review
    blocking = review.blocking_severities
    blocking_str = ", ".join(f"`{s}`" for s in blocking)

    nit_in_scale = "nit" in (blocking + (["nit"] if review.emit_nits else []))
    nit_scale_line = (
        "- **nit** (non-blocking): style, naming preference, minor readability."
        if review.emit_nits
        else ""
    )
    emit_nits_antipattern = (
        ""
        if review.emit_nits
        else "- Do not emit `nit`-severity findings; this repo has nits disabled."
    )

    if review.require_calibration:
        calibration_procedure = (
            "5. Severity calibration (mandatory). Before emitting any\n"
            "   finding whose severity is in " + blocking_str + ", write a\n"
            "   one-line justification in the finding's `_calibration` field\n"
            '   answering: "would a human reviewer genuinely block this PR\n'
            '   over this?" If your honest answer is "no" or "not sure,"\n'
            "   demote the severity. Reviews that block on weak reasoning\n"
            "   poison trust in the bot."
        )
        calibration_field = (
            ',\n      "_calibration": "Would a human block? Yes/no/why."'
        )
    else:
        calibration_procedure = ""
        calibration_field = ""

    if review.noteworthy_max > 0:
        noteworthy_field = (
            ',\n  "noteworthy": [\n'
            f'    "Optional. Up to {review.noteworthy_max} short callouts for'
            ' genuinely standout improvements. Pad nothing."\n'
            "  ]"
        )
    else:
        noteworthy_field = ""

    if review.run_id_scheme == "alpha":
        id_example = f"BOT-{run_id}1"
        id_scheme = (
            f"Use `BOT-{run_id}N` where N is sequential within this run "
            "(1, 2, 3, ...). When carrying a prior finding forward "
            "unchanged, reuse its existing ID."
        )
    elif review.run_id_scheme == "numeric":
        id_example = f"BOT-{run_id}-1"
        id_scheme = (
            f"Use `BOT-{run_id}-N` where N is sequential within this run."
        )
    else:
        id_example = "BOT-1"
        id_scheme = "Use `BOT-N` where N is sequential within this run."

    policy = config.post.first_review_approve_policy
    if policy == "never":
        first_review_rule = (
            "On the first review of a PR (no prior bot reviews), do not emit "
            "`APPROVE` unless `findings` is empty or contains only nits."
        )
    elif policy == "if_no_findings":
        first_review_rule = (
            "On the first review of a PR, emit `APPROVE` only if `findings` "
            "is empty."
        )
    else:
        first_review_rule = (
            "On the first review of a PR, `APPROVE` is allowed when no "
            "blocking findings are present."
        )

    if policy == "never":
        approve_rule = (
            "(a) only non-blocking findings are present AND this is not the "
            "first review of the PR, OR (b) all prior findings are now "
            "fixed/declined"
        )
    else:
        approve_rule = (
            "(a) only non-blocking findings are present, OR (b) all prior "
            "findings are now fixed/declined"
        )

    return {
        "REPO_EMPHASIS": review.repo_emphasis or "(none configured for this repo)",
        "CALIBRATION_PROCEDURE": calibration_procedure,
        "BLOCKING_SEVERITIES": blocking_str,
        "NIT_SCALE_LINE": nit_scale_line,
        "EMIT_NITS_ANTIPATTERN": emit_nits_antipattern,
        "MAX_FINDINGS": str(review.max_findings),
        "ID_EXAMPLE": id_example,
        "CALIBRATION_FIELD": calibration_field,
        "NOTEWORTHY_FIELD": noteworthy_field,
        "APPROVE_RULE": approve_rule,
        "FIRST_REVIEW_APPROVE_RULE": first_review_rule,
        "ID_SCHEME": id_scheme,
    }


def _find_unsubstituted(text: str) -> list[str]:
    import re

    return sorted(set(re.findall(r"<<([A-Z_][A-Z0-9_]*)>>", text)))
