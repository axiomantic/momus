"""Unit tests for momus.publish self-PR handling and 422 retry."""

from __future__ import annotations

from typing import Any

import pytest
import tripwire
from momus import publish as publish_mod
from momus.config import (
    ChecksConfig,
    Config,
    ConventionsConfig,
    PostConfig,
    ProviderConfig,
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
        verify=VerifyConfig(enabled=True),
        checks=ChecksConfig(enabled=False, name="Momus Code Review"),
        provider=ProviderConfig(model="", base_url=""),
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
    expected_body = expected_note + _expected_rendered_body(findings, run_url="https://run/1")
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
    expected_body = expected_note + _expected_rendered_body(findings, run_url="https://run/1")
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


def test_opaque_installation_token_sends_approve_unchanged():
    """Installation tokens (default or custom App) make `gh api /user` 4xx,
    so `_get_token_user_info` returns None and `_approve_downgrade_reason`
    cannot decide. APPROVE goes through to GitHub; `_submit_review`'s 422
    handler covers the default-token case.
    """
    cfg = _minimal_config()
    pr_meta = _pr_meta(author="some-human")
    findings = _findings_doc(verdict="APPROVE")

    gh_api = tripwire.mock.object(publish_mod, "_gh_api")
    gh_api.returns({})
    user_info = tripwire.mock.object(publish_mod, "_get_token_user_info")
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


def test_422_app_cannot_approve_retries_as_comment_with_note():
    """Default Actions GITHUB_TOKEN: GitHub returns 422 saying installations
    must use COMMENT/REQUEST_CHANGES. Retry with event=COMMENT, prepended
    downgrade note, AND the original inline comments preserved.
    """
    err = publish_mod._GhApiError(
        422,
        "HTTP 422: Validation Failed: GitHub Apps must use one of the events "
        "`COMMENT` or `REQUEST_CHANGES`",
    )
    gh_api = tripwire.mock.object(publish_mod, "_gh_api")
    gh_api.raises(err).returns({})

    inline = [{"path": "f.py", "line": 1, "side": "RIGHT", "body": "x"}]
    with tripwire:
        publish_mod._submit_review(
            owner="elijahr",
            repo="lockfreequeues",
            pr_number=25,
            head_sha="abc",
            body="original body",
            inline_comments=inline,
            event="APPROVE",
        )

    from dirty_equals import IsInstance

    gh_api.assert_call(
        args=(
            "POST",
            _reviews_endpoint(),
            {
                "commit_id": "abc",
                "body": "original body",
                "event": "APPROVE",
                "comments": inline,
            },
        ),
        kwargs={},
        raised=IsInstance(publish_mod._GhApiError),
    )
    expected_retry_body = (
        "_Note: verdict was APPROVE but downgraded to COMMENT because the "
        "default GITHUB_TOKEN cannot approve PRs (configure a GitHub App; "
        "see SETUP.md)._\n\noriginal body"
    )
    gh_api.assert_call(
        args=(
            "POST",
            _reviews_endpoint(),
            {
                "commit_id": "abc",
                "body": expected_retry_body,
                "event": "COMMENT",
                "comments": inline,
            },
        ),
        kwargs={},
    )


def test_422_cannot_approve_apps_phrasing_also_retries():
    """Liberal matcher: any 422 mentioning "cannot approve" alongside an
    Apps/installation hint should also trigger the COMMENT retry. Guards
    against GitHub rephrasing the error in the future.
    """
    err = publish_mod._GhApiError(
        422,
        "HTTP 422: GitHub Apps cannot approve their own pull request",
    )
    gh_api = tripwire.mock.object(publish_mod, "_gh_api")
    gh_api.raises(err).returns({})

    with tripwire:
        publish_mod._submit_review(
            owner="elijahr",
            repo="lockfreequeues",
            pr_number=25,
            head_sha="abc",
            body="b",
            inline_comments=[],
            event="APPROVE",
        )

    from dirty_equals import IsInstance

    gh_api.assert_call(
        args=(
            "POST",
            _reviews_endpoint(),
            {
                "commit_id": "abc",
                "body": "b",
                "event": "APPROVE",
                "comments": [],
            },
        ),
        kwargs={},
        raised=IsInstance(publish_mod._GhApiError),
    )
    expected_retry_body = (
        "_Note: verdict was APPROVE but downgraded to COMMENT because the "
        "default GITHUB_TOKEN cannot approve PRs (configure a GitHub App; "
        "see SETUP.md)._\n\nb"
    )
    gh_api.assert_call(
        args=(
            "POST",
            _reviews_endpoint(),
            {
                "commit_id": "abc",
                "body": expected_retry_body,
                "event": "COMMENT",
                "comments": [],
            },
        ),
        kwargs={},
    )


def test_422_self_approval_message_reraises_no_retry():
    """A 422 whose message indicates self-approval must re-raise without retry."""
    err = publish_mod._GhApiError(422, "HTTP 422: Can not approve your own pull request")
    gh_api = tripwire.mock.object(publish_mod, "_gh_api")
    gh_api.raises(err)

    with tripwire, pytest.raises(publish_mod._GhApiError) as excinfo:
        publish_mod._submit_review(
            owner="elijahr",
            repo="lockfreequeues",
            pr_number=25,
            head_sha="abc",
            body="b",
            inline_comments=[{"path": "f.py", "line": 1, "side": "RIGHT", "body": "x"}],
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
                "comments": [{"path": "f.py", "line": 1, "side": "RIGHT", "body": "x"}],
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
            inline_comments=[{"path": "f.py", "line": 1, "side": "RIGHT", "body": "x"}],
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
                "comments": [{"path": "f.py", "line": 1, "side": "RIGHT", "body": "x"}],
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

    with tripwire, pytest.raises(publish_mod._GhApiError) as excinfo:
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
    expected_body = expected_note + _expected_rendered_body(findings, run_url="https://run/1")
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


# --- Attribution + commands footer ------------------------------------------


def test_attribution_includes_model_and_host(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "deepseek/deepseek-v4-pro")
    monkeypatch.setenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    line = publish_mod._attribution_line()
    assert "[Momus](https://github.com/axiomantic/momus)" in line
    assert "`deepseek/deepseek-v4-pro`" in line
    assert "openrouter.ai" in line
    # The path component must be stripped — show only the host.
    assert "/api/v1" not in line


def test_attribution_with_model_only(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    line = publish_mod._attribution_line()
    assert "`claude-sonnet-4-6`" in line
    assert "via" not in line


def test_attribution_with_neither(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    line = publish_mod._attribution_line()
    assert "[Momus](https://github.com/axiomantic/momus)" in line
    # No "running ..." clause when there's nothing to show.
    assert "running" not in line


def test_commands_footer_default_trigger(monkeypatch):
    monkeypatch.delenv("MOMUS_TRIGGER_COMMAND", raising=False)
    monkeypatch.delenv("MOMUS_TRIGGER_MENTION", raising=False)
    footer = publish_mod._commands_footer()
    assert "`/ai-review`" in footer


def test_commands_footer_custom_trigger(monkeypatch):
    monkeypatch.setenv("MOMUS_TRIGGER_COMMAND", "/momus")
    monkeypatch.setenv("MOMUS_TRIGGER_MENTION", "@axiomantic-momus[bot]")
    footer = publish_mod._commands_footer()
    assert "`/momus`" in footer
    assert "@axiomantic-momus[bot]" in footer
    # The literal default must not leak through.
    assert "/ai-review" not in footer


def test_review_body_contains_attribution(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "deepseek/deepseek-v4-pro")
    monkeypatch.setenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    body = publish_mod.render_review_body(
        {"verdict": "COMMENT", "summary": "ok", "findings": []},
        run_url="https://x/run/1",
        run_id="A",
        config=_minimal_config(),
    )
    assert "Powered by [Momus]" in body
    assert "deepseek/deepseek-v4-pro" in body


# ---------------------------------------------------------------------------
# Cost footer: per-PR LLM spend rendered from per-phase usage totals
# ---------------------------------------------------------------------------


def _usage(cost: float, in_t: int, out_t: int, model: str = "m") -> Any:
    from momus.invoke_pi import PhaseUsage

    return PhaseUsage(
        cost_usd=cost,
        input_tokens=in_t,
        output_tokens=out_t,
        cached_tokens=0,
        model=model,
    )


def test_cost_footer_sums_phases_and_rounds_to_cents():
    body = publish_mod.render_review_body(
        {"verdict": "COMMENT", "summary": "ok", "findings": []},
        run_url="https://x/run/1",
        run_id="A",
        config=_minimal_config(),
        phase_usages=[
            ("phase1", _usage(0.0123, 100, 30, "deepseek/deepseek-v4-pro")),
            ("phase2", _usage(0.4019, 50000, 1500, "deepseek/deepseek-v4-pro")),
            ("phase3", _usage(0.0058, 8000, 200, "deepseek/deepseek-v4-pro")),
        ],
    )
    # Total cost: 0.0123 + 0.4019 + 0.0058 = 0.42 (rounded to cents).
    assert "Cost: $0.42" in body
    # Tokens summed and comma-grouped.
    assert "58,100 in / 1,730 out tokens" in body
    assert "deepseek/deepseek-v4-pro" in body


def test_cost_footer_omitted_when_no_usage():
    # No phase_usages and no tokens -> footer line is suppressed entirely.
    body = publish_mod.render_review_body(
        {"verdict": "COMMENT", "summary": "ok", "findings": []},
        run_url="https://x/run/1",
        run_id="A",
        config=_minimal_config(),
        phase_usages=[],
    )
    assert "Cost:" not in body


def test_cost_footer_omitted_when_tokens_zero():
    # Zero-token phases (e.g. all phases aborted before first call) -> no
    # cost line; tokens-of-zero is the signal that pricing is unreliable.
    body = publish_mod.render_review_body(
        {"verdict": "COMMENT", "summary": "ok", "findings": []},
        run_url="https://x/run/1",
        run_id="A",
        config=_minimal_config(),
        phase_usages=[("phase2", _usage(0.0, 0, 0))],
    )
    assert "Cost:" not in body


def test_cost_footer_renders_subcent_as_zero():
    # Sub-cent total still renders ($0.00) because tokens were consumed.
    # User asked for cents-rounded explicitly; $0.00 is the honest answer.
    body = publish_mod.render_review_body(
        {"verdict": "COMMENT", "summary": "ok", "findings": []},
        run_url="https://x/run/1",
        run_id="A",
        config=_minimal_config(),
        phase_usages=[("phase2", _usage(0.0019, 100, 20))],
    )
    assert "Cost: $0.00" in body
    assert "100 in / 20 out tokens" in body


def test_cost_footer_omits_model_when_blank():
    # When LLM_MODEL env was unset (PhaseUsage.model == ""), don't render
    # the trailing " - " separator that would leave an orphan dash.
    body = publish_mod.render_review_body(
        {"verdict": "COMMENT", "summary": "ok", "findings": []},
        run_url="https://x/run/1",
        run_id="A",
        config=_minimal_config(),
        phase_usages=[("phase2", _usage(0.10, 1000, 200, model=""))],
    )
    assert "Cost: $0.10 - 1,000 in / 200 out tokens_" in body
