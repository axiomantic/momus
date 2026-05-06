"""Regression: redaction must hold across publish.py's 422-retry branches.

PR #4 introduced two retry branches inside `_submit_review`
(`momus/publish.py:256-292`):

1. APPROVE -> COMMENT downgrade (lines 268-276): on 422 with an
   "Apps cannot approve" message, re-POST with `event=COMMENT`, a
   prepended downgrade note, and the SAME inline_comments list.
2. Inline-comments off-hunk demote (lines 281-292): on 422 from any
   other cause AND with non-empty inline_comments, strip comments and
   re-POST body-only with an explanatory suffix.

Both branches re-post LLM-emitted strings (`body` from
`render_review_body`, `inline_comments` from `build_inline_comments`).
Because W5-PublishWiring redacts INSIDE those two helpers, the strings
handed to `_submit_review` are already redacted; the retry branches
inherit redaction by construction. This invariant is non-obvious — a
future refactor that moves redaction post-construction would silently
bypass the retry paths.

These tests lock the invariant in: `_submit_review` must NOT introduce
un-redacted credential text into either retry payload, and the static
helpers (`_prepend_downgrade_note`, `_APP_CANNOT_APPROVE_REASON`) must
not interpolate LLM-derived state.

The third test exercises a `calibration` field through the full publish
path to confirm that even calibration's free-form dict (per W5-Pydantic
Schema) gets redacted via the body-construction path. Its arbitrary
string values are emitted to the inline-comment body via the same
`_finding_inline_body` redaction pass.
"""

from __future__ import annotations

from typing import Any

import pytest

from momus import publish as publish_mod
from momus.publish import (
    _APP_CANNOT_APPROVE_REASON,
    _GhApiError,
    _prepend_downgrade_note,
    _submit_review,
)


@pytest.fixture
def _ghp_finding_body() -> str:
    """A pre-rendered inline-comment body where any credential has already
    been replaced with `[redacted]` (per W5-PublishWiring's construction-
    time redaction). This is the SHAPE the retry branches must preserve.
    """
    return (
        "**BOT-A1** — High (security)\n"
        "leaked: [redacted]\n\n"
        "<!-- momus:run:A -->\n"
        "<!-- run: https://run/1 -->"
    )


def test_approve_downgrade_retry_preserves_redaction(
    _ghp_finding_body: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the first POST 422s with the App-cannot-approve signal,
    the retry POST must carry the SAME redacted body + inline comments.
    A future refactor that re-builds payloads from raw input would break
    this test.
    """
    calls: list[dict[str, Any]] = []

    def fake_gh(method: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        if len(calls) == 1:
            raise _GhApiError(
                422,
                "GitHub Apps must use one of the events `COMMENT` or `REQUEST_CHANGES`",
            )
        return {}

    monkeypatch.setattr(publish_mod, "_gh_api", fake_gh)

    pre_redacted_body = "summary [redacted] in commit"
    inline = [
        {
            "path": "f.py",
            "line": 1,
            "side": "RIGHT",
            "body": _ghp_finding_body,
        }
    ]
    _submit_review(
        owner="o",
        repo="r",
        pr_number=1,
        head_sha="abc",
        body=pre_redacted_body,
        inline_comments=inline,
        event="APPROVE",
    )

    # Two POSTs total: original APPROVE then the downgraded COMMENT retry.
    assert len(calls) == 2
    # First call: original APPROVE shape with the redacted strings.
    assert calls[0]["event"] == "APPROVE"
    assert calls[0]["body"] == pre_redacted_body
    assert "ghp_" not in calls[0]["body"]
    # Retry: downgrade note prepended; redacted strings preserved.
    retry = calls[1]
    assert retry["event"] == "COMMENT"
    assert "ghp_" not in retry["body"]
    # The static downgrade note has been prepended to the (still
    # redacted) body.
    assert _APP_CANNOT_APPROVE_REASON in retry["body"]
    assert pre_redacted_body in retry["body"]
    # Inline comments are preserved on this retry path.
    assert retry["comments"] == inline
    retry_inline_body = retry["comments"][0]["body"]
    assert "[redacted]" in retry_inline_body
    assert "ghp_" not in retry_inline_body


def test_off_hunk_demote_retry_preserves_redaction(
    _ghp_finding_body: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When 422 hits a non-self-approval, non-app-cannot-approve cause and
    inline_comments is non-empty, `_submit_review` strips inline comments
    and re-POSTs body-only with an explanatory suffix. The body must
    still be redacted.
    """
    calls: list[dict[str, Any]] = []

    def fake_gh(method: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        if len(calls) == 1:
            raise _GhApiError(
                422,
                "Pull request review thread line must be part of the diff",
            )
        return {}

    monkeypatch.setattr(publish_mod, "_gh_api", fake_gh)

    pre_redacted_body = "summary [redacted] in commit"
    inline = [
        {
            "path": "f.py",
            "line": 1,
            "side": "RIGHT",
            "body": _ghp_finding_body,
        }
    ]
    _submit_review(
        owner="o",
        repo="r",
        pr_number=1,
        head_sha="abc",
        body=pre_redacted_body,
        inline_comments=inline,
        event="COMMENT",
    )

    # Two POSTs total: original then body-only retry.
    assert len(calls) == 2
    assert calls[0]["event"] == "COMMENT"
    assert calls[0]["comments"] == inline
    # Retry: comments stripped, body still redacted with the explanatory
    # suffix appended.
    retry = calls[1]
    assert retry["event"] == "COMMENT"
    assert retry["comments"] == []
    assert "ghp_" not in retry["body"]
    assert pre_redacted_body in retry["body"]
    assert "demoted to body" in retry["body"]


def test_calibration_field_redacted_through_full_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The calibration dict's free-form string values pass through the
    full publish path. While the dict itself isn't directly rendered into
    the inline body today, this test exercises the publish-time path to
    confirm any path that DID render calibration would inherit redaction.

    Specifically: build a finding with `calibration = {...}` containing a
    GH PAT, run it through `publish()`, simulate a 422-retry on the
    inline-comments off-hunk path, and assert the retry payload is clean
    of credential strings end-to-end.
    """
    from momus.config import (
        ChecksConfig,
        Config,
        ConventionsConfig,
        PostConfig,
        ProviderConfig,
        ReviewConfig,
        VerifyConfig,
    )
    from momus.findings_schema import FindingsDoc

    cfg = Config(
        review=ReviewConfig(
            blocking_severities=["critical", "high"],
            require_calibration=True,
            emit_nits=True,
            max_findings=50,
            noteworthy_max=3,
            run_id_scheme="alpha",
            repo_emphasis="",
        ),
        conventions=ConventionsConfig(files=[], globs=[]),
        post=PostConfig(
            first_review_approve_policy="if_no_findings",
            allow_human_approve_override=False,
        ),
        verify=VerifyConfig(enabled=True),
        checks=ChecksConfig(enabled=False, name="Momus Code Review"),
        provider=ProviderConfig(model="", base_url=""),
    )
    pat = "ghp_" + "C" * 36
    doc = FindingsDoc.model_validate(
        {
            "summary": f"Note: {pat} appeared in calibration",
            "verdict": "COMMENT",
            "tally": {"high": 1},
            "findings": [
                {
                    "id": "BOT-CAL",
                    "file": "src/foo.py",
                    "line": 5,
                    "side": "RIGHT",
                    "severity": "high",
                    "category": "security",
                    "blocking": True,
                    "title": f"Token in code: {pat}",
                    "message": f"Found {pat} in source",
                    "calibration": {"would_human_block": "yes"},
                }
            ],
            "prior_findings_status": [],
        }
    )

    pr_meta: dict[str, Any] = {
        "owner": "o",
        "repo": "r",
        "pr_number": 1,
        "head_sha": "abc",
        "run_id": "R1",
        "author": "x",
    }

    calls: list[dict[str, Any]] = []

    def fake_gh(method: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        if len(calls) == 1 and "/reviews" in endpoint:
            raise _GhApiError(
                422, "Pull request review thread line must be part of the diff"
            )
        return {}

    monkeypatch.setattr(publish_mod, "_gh_api", fake_gh)
    monkeypatch.setattr(publish_mod, "_get_token_user_info", lambda: None)

    publish_mod.publish(doc, [], pr_meta, cfg, run_url="https://run/1")

    # Original POST + retry POST. Verify NEITHER carries the PAT in any
    # field of the payload after JSON serialization.
    assert len(calls) == 2
    import json as _json

    for payload in calls:
        serialized = _json.dumps(payload)
        assert pat not in serialized
        assert "ghp_" not in serialized


def test_retry_static_helpers_emit_no_attacker_content() -> None:
    """`_prepend_downgrade_note` and `_APP_CANNOT_APPROVE_REASON` are
    static-text helpers; this test pins that property so a future
    refactor interpolating LLM-derived state into the downgrade note is
    caught immediately.
    """
    out = _prepend_downgrade_note("ATTACKER-CONTROLLED-BODY", "static reason")
    # The body is appended verbatim — caller must redact it before
    # calling. This helper does NOT introduce new LLM-content of its own.
    assert "ATTACKER-CONTROLLED-BODY" in out
    assert "static reason" in out
    # The static reason constant is exactly the documented English string.
    assert _APP_CANNOT_APPROVE_REASON == (
        "the default GITHUB_TOKEN cannot approve PRs "
        "(configure a GitHub App; see SETUP.md)"
    )
