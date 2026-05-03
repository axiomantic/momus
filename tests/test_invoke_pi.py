"""Unit tests for momus.invoke_pi retry guard."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import tripwire

from momus import invoke_pi as invoke_pi_mod
from momus.invoke_pi import (
    PHASE_EXPECTED_OUTPUTS,
    PiInvocationError,
    invoke_pi_phase,
    invoke_pi_phase_with_retry,
)


# Shared event line that pi would emit on stdout.
_EVENT_LINE = '{"type": "complete", "ok": true}'
_EVENT_PARSED = json.loads(_EVENT_LINE)


def _setup_work_dir(tmp_path: Path, phase: str) -> Path:
    """Create a work_dir with a stub prompt file and outputs/ subdir."""
    work_dir = tmp_path / "work"
    (work_dir / "inputs" / "prompts").mkdir(parents=True)
    (work_dir / "outputs").mkdir(parents=True)
    (work_dir / "inputs" / "prompts" / f"{phase}.md").write_text("stub prompt")
    return work_dir


class _FakePopen:
    """Minimal Popen stand-in supporting context manager + line iteration."""

    def __init__(self, returncode: int, stdout_text: str) -> None:
        self.returncode = returncode
        self.stdout = iter(stdout_text.splitlines(keepends=True))
        self.stderr = iter([])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def wait(self) -> int:
        return self.returncode


def _fake_pi_factory(
    work_dir: Path,
    expected_rel_path: str | None,
    write_on_calls: set[int],
    returncode: int = 0,
):
    """
    Build a `.calls()` side-effect for subprocess.Popen that simulates pi.

    ``write_on_calls`` is the set of 1-based call indices on which the fake
    pi will materialize the expected output file before returning. Each call
    is recorded into ``captured`` so tests can later assert exact cmd shape.
    """
    state = {"calls": 0}
    captured: list[tuple[tuple, dict]] = []

    def side_effect(*args, **kwargs):
        state["calls"] += 1
        call_idx = state["calls"]
        captured.append((args, kwargs))
        if expected_rel_path is not None and call_idx in write_on_calls:
            target = work_dir / expected_rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}")
        return _FakePopen(returncode=returncode, stdout_text=_EVENT_LINE + "\n")

    return side_effect, state, captured


@pytest.fixture(autouse=True)
def _llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """invoke_pi requires LLM_BASE_URL and LLM_MODEL to build the command."""
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("LLM_MODEL", "test-model")


def test_first_call_produces_expected_file_no_retry(tmp_path: Path) -> None:
    phase = "phase2"
    expected_rel = PHASE_EXPECTED_OUTPUTS[phase]
    work_dir = _setup_work_dir(tmp_path, phase)
    side_effect, state, captured = _fake_pi_factory(
        work_dir, expected_rel, write_on_calls={1}
    )

    run_mock = tripwire.mock.object(invoke_pi_mod.subprocess, "Popen")
    run_mock.calls(side_effect)

    with tripwire:
        events = invoke_pi_phase_with_retry(phase, work_dir)

    assert events == [_EVENT_PARSED]
    assert state["calls"] == 1
    assert len(captured) == 1
    # File exists, single call, prompt did NOT include the reminder.
    first_call_cmd = captured[0][0][0]
    prompt_idx = first_call_cmd.index("-p") + 1
    assert first_call_cmd[prompt_idx] == "stub prompt"

    args, kwargs = captured[0]
    run_mock.assert_call(args=args, kwargs=kwargs)


def test_first_call_missing_file_retry_succeeds(tmp_path: Path) -> None:
    phase = "phase2"
    expected_rel = PHASE_EXPECTED_OUTPUTS[phase]
    work_dir = _setup_work_dir(tmp_path, phase)
    # Only the second call writes the file.
    side_effect, state, captured = _fake_pi_factory(
        work_dir, expected_rel, write_on_calls={2}
    )

    run_mock = tripwire.mock.object(invoke_pi_mod.subprocess, "Popen")
    run_mock.calls(side_effect).calls(side_effect)

    with tripwire:
        events = invoke_pi_phase_with_retry(phase, work_dir)

    # Returned events come from the second (successful) call.
    assert events == [_EVENT_PARSED]
    assert state["calls"] == 2
    assert len(captured) == 2

    first_cmd = captured[0][0][0]
    second_cmd = captured[1][0][0]
    first_prompt = first_cmd[first_cmd.index("-p") + 1]
    second_prompt = second_cmd[second_cmd.index("-p") + 1]

    assert first_prompt == "stub prompt"
    expected_suffix = (
        "\n\n---\n"
        "CRITICAL REMINDER: You MUST invoke the `write_output` tool to write "
        f"`{expected_rel}`. Your previous attempt ended without writing this "
        "file. Do NOT respond with prose only; call `write_output` before "
        "ending your turn."
    )
    assert second_prompt == "stub prompt" + expected_suffix

    for args, kwargs in captured:
        run_mock.assert_call(args=args, kwargs=kwargs)


def test_retry_also_misses_file_raises(tmp_path: Path) -> None:
    phase = "phase2"
    expected_rel = PHASE_EXPECTED_OUTPUTS[phase]
    work_dir = _setup_work_dir(tmp_path, phase)
    # Neither call writes the file.
    side_effect, state, captured = _fake_pi_factory(
        work_dir, expected_rel, write_on_calls=set()
    )

    run_mock = tripwire.mock.object(invoke_pi_mod.subprocess, "Popen")
    run_mock.calls(side_effect).calls(side_effect)

    with tripwire:
        with pytest.raises(PiInvocationError) as exc_info:
            invoke_pi_phase_with_retry(phase, work_dir)

    assert state["calls"] == 2
    assert len(captured) == 2
    msg = str(exc_info.value)
    assert msg == (
        f"phase {phase} did not produce expected output {expected_rel} "
        "after retry"
    )

    for args, kwargs in captured:
        run_mock.assert_call(args=args, kwargs=kwargs)


def test_first_call_nonzero_exit_raises_no_retry(tmp_path: Path) -> None:
    phase = "phase2"
    expected_rel = PHASE_EXPECTED_OUTPUTS[phase]
    work_dir = _setup_work_dir(tmp_path, phase)
    side_effect, state, captured = _fake_pi_factory(
        work_dir, expected_rel, write_on_calls=set(), returncode=2
    )

    run_mock = tripwire.mock.object(invoke_pi_mod.subprocess, "Popen")
    run_mock.calls(side_effect)

    with tripwire:
        with pytest.raises(PiInvocationError) as exc_info:
            invoke_pi_phase_with_retry(phase, work_dir)

    # Pre-existing behavior: non-zero exit raises immediately, no retry.
    assert state["calls"] == 1
    assert len(captured) == 1
    msg = str(exc_info.value)
    assert f"phase {phase} failed with exit 2" in msg

    args, kwargs = captured[0]
    run_mock.assert_call(args=args, kwargs=kwargs)


def test_phase_not_in_expected_outputs_skips_guard(tmp_path: Path) -> None:
    """invoke_pi_phase (the unguarded variant) does not check files."""
    phase = "phase2"
    work_dir = _setup_work_dir(tmp_path, phase)
    # No file is ever written, but the unguarded function just returns events.
    side_effect, state, captured = _fake_pi_factory(
        work_dir, expected_rel_path=None, write_on_calls=set()
    )

    run_mock = tripwire.mock.object(invoke_pi_mod.subprocess, "Popen")
    run_mock.calls(side_effect)

    with tripwire:
        events = invoke_pi_phase(phase, work_dir)

    assert events == [_EVENT_PARSED]
    assert state["calls"] == 1
    assert len(captured) == 1

    args, kwargs = captured[0]
    run_mock.assert_call(args=args, kwargs=kwargs)


def test_phase_expected_outputs_mapping_is_exactly_three_phases() -> None:
    assert PHASE_EXPECTED_OUTPUTS == {
        "phase1": "outputs/prior-findings.json",
        "phase2": "outputs/findings.json",
        "phase3": "outputs/findings.json",
    }
