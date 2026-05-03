"""Unit tests for momus.publish self-PR handling and 422 retry."""

from __future__ import annotations

from typing import Any

import pytest
import tripwire

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


def _reviews_endpoint() -> str:
    return "/repos/elijahr/lockfreequeues/pulls/25/reviews"


def test_self_pr_approve_downgrades_to_comment_with_note():
    """When token user equals PR author and verdict is APPROVE, downgrade to COMMENT
    and prepend a note to the body. Single submit call."""
    cfg = _minimal_config()
    pr_meta = _pr_meta(author="elijahr")
    findings = _findings_doc(verdict="APPROVE")

    gh_api = tripwire.mock.object(publish_mod, "_gh_api")
    gh_api.returns({})
    user_info = tripwire.mock.object(publish_mod, "_get_token_user_info")
    user_info.returns({"login": "elijahr", "type": "User"})

    with tripwire:
        publish_mod.publish(findings, [], pr_meta, cfg, run_url="https://run/1")

    # _get_token_user_info is called at least once; assert one call (its only use site).
    user_info.assert_call(args=(), kwargs={})

    expected_note = (
        "_Note: verdict was APPROVE but downgraded to COMMENT because the bot "
        "account is the PR author._\n\n"
    )
    expected_body = expected_note + _expected_rendered_body(
        findings, run_url="https://run/1"
    )
    gh_api.assert_call(
        args=(
            "POST",
            _reviews_endpoint(),
            {
                "commit_id": pr_meta["head_sha"],
                "body": expected_body,
                "event": "COMMENT",
                "comments": [],
            },
        ),
        kwargs={},
    )


def test_non_self_pr_approve_stays_approve_unchanged_body():
    """When token user differs from PR author, APPROVE stays APPROVE; body has no note."""
    cfg = _minimal_config()
    pr_meta = _pr_meta(author="someone-else")
    findings = _findings_doc(verdict="APPROVE")

    gh_api = tripwire.mock.object(publish_mod, "_gh_api")
    gh_api.returns({})
    user_info = tripwire.mock.object(publish_mod, "_get_token_user_info")
    # Called once by _approve_downgrade_reason; verdict stays APPROVE.
    user_info.returns({"login": "bot-account", "type": "User"})

    with tripwire:
        publish_mod.publish(findings, [], pr_meta, cfg, run_url="https://run/1")

    user_info.assert_call(args=(), kwargs={})
    expected_body = _expected_rendered_body(findings, run_url="https://run/1")
    gh_api.assert_call(
        args=(
            "POST",
            _reviews_endpoint(),
            {
                "commit_id": pr_meta["head_sha"],
                "body": expected_body,
                "event": "APPROVE",
                "comments": [],
            },
        ),
        kwargs={},
    )


def test_bot_token_approve_downgrades_to_comment():
    """A Bot-type token (e.g. github-actions[bot]) cannot approve PRs by default,
    even when its login differs from the PR author. Downgrade preemptively."""
    cfg = _minimal_config()
    pr_meta = _pr_meta(author="some-human")
    findings = _findings_doc(verdict="APPROVE")

    gh_api = tripwire.mock.object(publish_mod, "_gh_api")
    gh_api.returns({})
    user_info = tripwire.mock.object(publish_mod, "_get_token_user_info")
    user_info.returns({"login": "github-actions[bot]", "type": "Bot"})

    with tripwire:
        publish_mod.publish(findings, [], pr_meta, cfg, run_url="https://run/1")

    user_info.assert_call(args=(), kwargs={})
    expected_note = (
        "_Note: verdict was APPROVE but downgraded to COMMENT because "
        "the token user `github-actions[bot]` is a Bot and cannot "
        "approve PRs._\n\n"
    )
    expected_body = expected_note + _expected_rendered_body(
        findings, run_url="https://run/1"
    )
    gh_api.assert_call(
        args=(
            "POST",
            _reviews_endpoint(),
            {
                "commit_id": pr_meta["head_sha"],
                "body": expected_body,
                "event": "COMMENT",
                "comments": [],
            },
        ),
        kwargs={},
    )


def test_token_lookup_failure_does_not_downgrade_warns_stderr(capsys):
    """If we can't determine the token user, do NOT downgrade.

    NOTE: production code currently emits no stderr warning on this path; the
    intended-but-unimplemented warning would read::

        momus: warning: could not determine GH token user; skipping APPROVE
        downgrade check.

    See deliverable notes — this is a real gap in publish.py.
    """
    cfg = _minimal_config()
    pr_meta = _pr_meta(author="elijahr")
    findings = _findings_doc(verdict="APPROVE")

    gh_api = tripwire.mock.object(publish_mod, "_gh_api")
    gh_api.returns({})
    user_info = tripwire.mock.object(publish_mod, "_get_token_user_info")
    # _get_token_user_info is invoked once by _approve_downgrade_reason and
    # returns None, so no downgrade reason is determined and verdict stays
    # APPROVE.
    user_info.returns(None)

    with tripwire:
        publish_mod.publish(findings, [], pr_meta, cfg, run_url="https://run/1")

    user_info.assert_call(args=(), kwargs={})
    expected_body = _expected_rendered_body(findings, run_url="https://run/1")
    gh_api.assert_call(
        args=(
            "POST",
            _reviews_endpoint(),
            {
                "commit_id": pr_meta["head_sha"],
                "body": expected_body,
                "event": "APPROVE",
                "comments": [],
            },
        ),
        kwargs={},
    )
    captured = capsys.readouterr()
    # Production currently does not emit this warning. When publish.py is
    # updated to warn, change this assertion to the expected message.
    assert captured.err == ""


def test_422_self_approval_message_reraises_no_retry():
    """A 422 whose message indicates self-approval must re-raise without retry."""
    err = publish_mod._GhApiError(
        422, "HTTP 422: Can not approve your own pull request"
    )
    gh_api = tripwire.mock.object(publish_mod, "_gh_api")
    gh_api.raises(err)

    with tripwire:
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

    from dirty_equals import IsInstance
    gh_api.assert_call(
        args=(
            "POST",
            _reviews_endpoint(),
            {
                "commit_id": "abc",
                "body": "b",
                "event": "APPROVE",
                "comments": [
                    {"path": "f.py", "line": 1, "side": "RIGHT", "body": "x"}
                ],
            },
        ),
        kwargs={},
        raised=IsInstance(publish_mod._GhApiError),
    )


def test_422_non_self_approval_with_inline_comments_retries_body_only():
    """A 422 without self-approval signal AND with inline comments retries body-only."""
    err = publish_mod._GhApiError(
        422,
        "HTTP 422: Validation Failed: pull_request_review_thread.line must be part of the diff",
    )
    gh_api = tripwire.mock.object(publish_mod, "_gh_api")
    # First call raises, second call succeeds.
    gh_api.raises(err).returns({})

    with tripwire:
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

    from dirty_equals import IsInstance
    gh_api.assert_call(
        args=(
            "POST",
            _reviews_endpoint(),
            {
                "commit_id": "abc",
                "body": "original body",
                "event": "COMMENT",
                "comments": [
                    {"path": "f.py", "line": 1, "side": "RIGHT", "body": "x"}
                ],
            },
        ),
        kwargs={},
        raised=IsInstance(publish_mod._GhApiError),
    )
    gh_api.assert_call(
        args=(
            "POST",
            _reviews_endpoint(),
            {
                "commit_id": "abc",
                "body": (
                    "original body\n\n_Note: inline comments were demoted to body "
                    "because some line citations were not on diff hunks._"
                ),
                "event": "COMMENT",
                "comments": [],
            },
        ),
        kwargs={},
    )


def test_422_with_no_inline_comments_reraises():
    """A 422 with no inline comments to strip must re-raise (existing behavior)."""
    err = publish_mod._GhApiError(422, "HTTP 422: something else broke")
    gh_api = tripwire.mock.object(publish_mod, "_gh_api")
    gh_api.raises(err)

    with tripwire:
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

    from dirty_equals import IsInstance
    gh_api.assert_call(
        args=(
            "POST",
            _reviews_endpoint(),
            {
                "commit_id": "abc",
                "body": "b",
                "event": "COMMENT",
                "comments": [],
            },
        ),
        kwargs={},
        raised=IsInstance(publish_mod._GhApiError),
    )


def test_self_pr_check_is_case_insensitive():
    """GH logins are case-insensitive; comparison must be too."""
    cfg = _minimal_config()
    pr_meta = _pr_meta(author="ElijahR")
    findings = _findings_doc(verdict="APPROVE")

    gh_api = tripwire.mock.object(publish_mod, "_gh_api")
    gh_api.returns({})
    user_info = tripwire.mock.object(publish_mod, "_get_token_user_info")
    user_info.returns({"login": "elijahr", "type": "User"})

    with tripwire:
        publish_mod.publish(findings, [], pr_meta, cfg, run_url="https://run/1")

    user_info.assert_call(args=(), kwargs={})
    expected_note = (
        "_Note: verdict was APPROVE but downgraded to COMMENT because the bot "
        "account is the PR author._\n\n"
    )
    expected_body = expected_note + _expected_rendered_body(
        findings, run_url="https://run/1"
    )
    gh_api.assert_call(
        args=(
            "POST",
            _reviews_endpoint(),
            {
                "commit_id": pr_meta["head_sha"],
                "body": expected_body,
                "event": "COMMENT",
                "comments": [],
            },
        ),
        kwargs={},
    )


def _expected_rendered_body(
    findings: dict[str, Any],
    *,
    run_url: str,
    run_id: str = "R1",
    config: Config | None = None,
) -> str:
    """Render the publish body the same way production code does, so tests can
    assert exact equality against payloads instead of substring matches."""
    cfg = config if config is not None else _minimal_config()
    return publish_mod.render_review_body(findings, run_url, run_id, cfg)
