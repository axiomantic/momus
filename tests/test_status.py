"""Unit tests for the sticky-status comment renderer + upsert path."""

from __future__ import annotations

import json
import subprocess

import pytest
import tripwire

from momus import status as status_mod
from momus.status import STATUS_GIF_URL, STATUS_MARKER, post_status


# --- _render_body ------------------------------------------------------------


def test_render_body_in_progress_states_include_gif() -> None:
    for state in ("starting", "running", "posting"):
        body = status_mod._render_body(state=state, detail="phase X", run_url="")
        assert STATUS_GIF_URL in body, f"{state} should embed the working GIF"
        assert STATUS_MARKER in body
        assert state.lower() in body.lower() or "momus" in body.lower()


def test_render_body_terminal_states_omit_gif() -> None:
    for state in ("done", "failed"):
        body = status_mod._render_body(state=state, detail="ok", run_url="")
        assert STATUS_GIF_URL not in body, f"{state} must not show the working GIF"
        assert STATUS_MARKER in body


def test_render_body_includes_run_url_when_provided() -> None:
    body = status_mod._render_body(
        state="running", detail="phase 2/3", run_url="https://x/run/1"
    )
    assert "[run log](https://x/run/1)" in body


def test_render_body_failed_includes_detail() -> None:
    body = status_mod._render_body(
        state="failed", detail="phase phase2 failed with exit 2", run_url=""
    )
    assert "Momus review failed" in body
    assert "phase phase2 failed with exit 2" in body


# --- post_status (mocked subprocess) ----------------------------------------


def _gh_proc(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=""
    )


def _captured_run_factory():
    """
    Build a side-effect for ``subprocess.run`` that records every call
    and returns a queued response.

    Tripwire does not expose call history, so we capture into a list
    and assert on it explicitly.
    """
    captured: list[tuple[tuple, dict]] = []
    queue: list[subprocess.CompletedProcess] = []

    def side_effect(*args, **kwargs):
        captured.append((args, kwargs))
        if not queue:
            raise AssertionError("subprocess.run called more times than queued")
        return queue.pop(0)

    return side_effect, captured, queue


def test_post_status_creates_comment_when_none_exists() -> None:
    side_effect, captured, queue = _captured_run_factory()
    queue.append(_gh_proc(stdout="[]"))           # list -> empty
    queue.append(_gh_proc(stdout='{"id": 999}'))  # create

    run_mock = tripwire.mock.object(status_mod.subprocess, "run")
    run_mock.calls(side_effect).calls(side_effect)

    with tripwire:
        post_status(
            owner="o",
            repo="r",
            pr_number=42,
            state="starting",
            detail="setting up",
            run_url="",
        )
    assert len(captured) == 2
    for args, kwargs in captured:
        run_mock.assert_call(args=args, kwargs=kwargs)
    list_argv = captured[0][0][0]
    create_argv = captured[1][0][0]
    assert "/repos/o/r/issues/42/comments" in " ".join(list_argv)
    assert "POST" in create_argv
    assert "/repos/o/r/issues/42/comments" in " ".join(create_argv)
    posted_body = json.loads(captured[1][1]["input"])["body"]
    assert STATUS_MARKER in posted_body
    assert "setting up" in posted_body


def test_post_status_updates_existing_comment_when_marker_found() -> None:
    existing = json.dumps(
        [
            {"id": 111, "body": "unrelated"},
            {"id": 222, "body": f"earlier status {STATUS_MARKER}"},
        ]
    )
    side_effect, captured, queue = _captured_run_factory()
    queue.append(_gh_proc(stdout=existing))       # list
    queue.append(_gh_proc(stdout='{"id": 222}'))  # update

    run_mock = tripwire.mock.object(status_mod.subprocess, "run")
    run_mock.calls(side_effect).calls(side_effect)

    with tripwire:
        post_status(
            owner="o",
            repo="r",
            pr_number=42,
            state="running",
            detail="phase 2/3",
            run_url="",
        )
    assert len(captured) == 2
    for args, kwargs in captured:
        run_mock.assert_call(args=args, kwargs=kwargs)
    update_argv = captured[1][0][0]
    assert "PATCH" in update_argv
    assert "/repos/o/r/issues/comments/222" in " ".join(update_argv)
    patched_body = json.loads(captured[1][1]["input"])["body"]
    assert STATUS_MARKER in patched_body
    assert "phase 2/3" in patched_body


def test_post_status_swallows_failures() -> None:
    """A failing gh call MUST NOT raise — review run takes priority."""
    captured: list[tuple[tuple, dict]] = []

    def side_effect(*args, **kwargs):
        captured.append((args, kwargs))
        raise RuntimeError("boom")

    run_mock = tripwire.mock.object(status_mod.subprocess, "run")
    run_mock.calls(side_effect)

    with tripwire:
        # Should NOT raise.
        post_status(
            owner="o",
            repo="r",
            pr_number=42,
            state="failed",
            detail="bad",
            run_url="",
        )
    assert len(captured) == 1
    for args, kwargs in captured:
        run_mock.assert_call(args=args, kwargs=kwargs)
