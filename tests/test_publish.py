"""Unit tests for momus.publish self-PR handling and 422 retry."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from momus import publish as publish_mod
from momus.config import (
    Config,
    ConventionsConfig,
    PostConfig,
    ReviewConfig,
    VerifyConfig,
)


def _minimal_config() -> Config:
    return Config(
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
        verify=VerifyConfig(enabled=True, skip_post_on_empty=True),
    )


def _pr_meta(author: str = "someone-else") -> dict[str, Any]:
    return {
        "owner": "elijahr",
        "repo": "lockfreequeues",
        "pr_number": 25,
        "head_sha": "deadbeef" + "0" * 32,
        "run_id": "R1",
        "author": author,
    }


def _findings_doc(verdict: str = "APPROVE") -> dict[str, Any]:
    return {
        "verdict": verdict,
        "summary": "All good.",
        "tally": {"low": 2},
        "findings": [],
        "noteworthy": [],
        "prior_findings_status": [],
    }


def _captured_payload(mock_gh_api):
    """Extract the last submit-review payload kwarg/arg passed to _gh_api."""
    assert mock_gh_api.call_count >= 1
    last_call = mock_gh_api.call_args_list[-1]
    args = last_call.args
    # _gh_api(method, endpoint, payload)
    return args[0], args[1], args[2]


def test_self_pr_approve_downgrades_to_comment_with_note():
    """When token user equals PR author and verdict is APPROVE, downgrade to COMMENT
    and prepend a note to the body. Single submit call."""
    cfg = _minimal_config()
    pr_meta = _pr_meta(author="elijahr")
    findings = _findings_doc(verdict="APPROVE")

    with patch.object(publish_mod, "_gh_api", return_value={}) as mock_api, patch.object(
        publish_mod, "_get_token_login", return_value="elijahr"
    ) as mock_login:
        publish_mod.publish(findings, [], pr_meta, cfg, run_url="https://run/1")

    assert mock_login.call_count == 1
    assert mock_api.call_count == 1
    method, endpoint, payload = _captured_payload(mock_api)
    assert method == "POST"
    assert endpoint == "/repos/elijahr/lockfreequeues/pulls/25/reviews"
    assert payload["event"] == "COMMENT"
    expected_note = (
        "_Note: verdict was APPROVE but downgraded to COMMENT because the bot "
        "account is the PR author._\n\n"
    )
    assert payload["body"].startswith(expected_note)
    # And the original rendered body is still present after the note.
    assert "**Verdict:** APPROVE." in payload["body"]


def test_non_self_pr_approve_stays_approve_unchanged_body():
    """When token user differs from PR author, APPROVE stays APPROVE; body has no note."""
    cfg = _minimal_config()
    pr_meta = _pr_meta(author="someone-else")
    findings = _findings_doc(verdict="APPROVE")

    with patch.object(publish_mod, "_gh_api", return_value={}) as mock_api, patch.object(
        publish_mod, "_get_token_login", return_value="bot-account"
    ):
        publish_mod.publish(findings, [], pr_meta, cfg, run_url="https://run/1")

    assert mock_api.call_count == 1
    _method, _endpoint, payload = _captured_payload(mock_api)
    assert payload["event"] == "APPROVE"
    assert "downgraded to COMMENT" not in payload["body"]


def test_token_lookup_failure_does_not_downgrade_warns_stderr(capsys):
    """If we can't determine the token login, do NOT downgrade. Warn to stderr."""
    cfg = _minimal_config()
    pr_meta = _pr_meta(author="elijahr")
    findings = _findings_doc(verdict="APPROVE")

    with patch.object(publish_mod, "_gh_api", return_value={}) as mock_api, patch.object(
        publish_mod, "_get_token_login", return_value=None
    ):
        publish_mod.publish(findings, [], pr_meta, cfg, run_url="https://run/1")

    assert mock_api.call_count == 1
    _method, _endpoint, payload = _captured_payload(mock_api)
    assert payload["event"] == "APPROVE"
    assert "downgraded to COMMENT" not in payload["body"]
    captured = capsys.readouterr()
    assert (
        captured.err.strip()
        == "momus: warning: could not determine GH token login; "
        "skipping self-PR check."
    )


def test_422_self_approval_message_reraises_no_retry():
    """A 422 whose message indicates self-approval must re-raise without retry."""
    err = publish_mod._GhApiError(
        422, "HTTP 422: Can not approve your own pull request"
    )
    with patch.object(publish_mod, "_gh_api", side_effect=err) as mock_api:
        with pytest.raises(publish_mod._GhApiError) as excinfo:
            publish_mod._submit_review(
                owner="elijahr",
                repo="lockfreequeues",
                pr_number=25,
                head_sha="abc",
                body="b",
                inline_comments=[
                    {"path": "f.py", "line": 1, "side": "RIGHT", "body": "x"}
                ],
                event="APPROVE",
            )
    assert excinfo.value.status == 422
    assert mock_api.call_count == 1


def test_422_non_self_approval_with_inline_comments_retries_body_only():
    """A 422 without self-approval signal AND with inline comments retries body-only."""
    err = publish_mod._GhApiError(
        422,
        "HTTP 422: Validation Failed: pull_request_review_thread.line must be part of the diff",
    )
    # First call raises, second call succeeds.
    with patch.object(
        publish_mod, "_gh_api", side_effect=[err, {}]
    ) as mock_api:
        publish_mod._submit_review(
            owner="elijahr",
            repo="lockfreequeues",
            pr_number=25,
            head_sha="abc",
            body="original body",
            inline_comments=[
                {"path": "f.py", "line": 1, "side": "RIGHT", "body": "x"}
            ],
            event="COMMENT",
        )

    assert mock_api.call_count == 2
    first_args = mock_api.call_args_list[0].args
    second_args = mock_api.call_args_list[1].args
    assert first_args[0] == "POST"
    assert first_args[1] == "/repos/elijahr/lockfreequeues/pulls/25/reviews"
    assert first_args[2] == {
        "commit_id": "abc",
        "body": "original body",
        "event": "COMMENT",
        "comments": [
            {"path": "f.py", "line": 1, "side": "RIGHT", "body": "x"}
        ],
    }
    assert second_args[0] == "POST"
    assert second_args[1] == "/repos/elijahr/lockfreequeues/pulls/25/reviews"
    assert second_args[2] == {
        "commit_id": "abc",
        "body": (
            "original body\n\n_Note: inline comments were demoted to body "
            "because some line citations were not on diff hunks._"
        ),
        "event": "COMMENT",
        "comments": [],
    }


def test_422_with_no_inline_comments_reraises():
    """A 422 with no inline comments to strip must re-raise (existing behavior)."""
    err = publish_mod._GhApiError(422, "HTTP 422: something else broke")
    with patch.object(publish_mod, "_gh_api", side_effect=err) as mock_api:
        with pytest.raises(publish_mod._GhApiError) as excinfo:
            publish_mod._submit_review(
                owner="elijahr",
                repo="lockfreequeues",
                pr_number=25,
                head_sha="abc",
                body="b",
                inline_comments=[],
                event="COMMENT",
            )
    assert excinfo.value.status == 422
    assert mock_api.call_count == 1


def test_self_pr_check_is_case_insensitive():
    """GH logins are case-insensitive; comparison must be too."""
    cfg = _minimal_config()
    pr_meta = _pr_meta(author="ElijahR")
    findings = _findings_doc(verdict="APPROVE")

    with patch.object(publish_mod, "_gh_api", return_value={}) as mock_api, patch.object(
        publish_mod, "_get_token_login", return_value="elijahr"
    ):
        publish_mod.publish(findings, [], pr_meta, cfg, run_url="https://run/1")

    _method, _endpoint, payload = _captured_payload(mock_api)
    assert payload["event"] == "COMMENT"
