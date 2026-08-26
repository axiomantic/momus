"""Unit tests for momus.invoke_pi retry guard."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import tripwire
from momus import invoke_pi as invoke_pi_mod
from momus.invoke_pi import (
    PHASE_EXPECTED_OUTPUTS,
    PhaseUsage,
    PiInvocationError,
    invoke_pi_phase,
    invoke_pi_phase_with_retry,
    summarize_usage,
)

# Shared event line that pi would emit on stdout.
_EVENT_LINE = '{"type": "complete", "ok": true}'
_EVENT_PARSED = json.loads(_EVENT_LINE)


def _setup_work_dir(tmp_path: Path, phase: str) -> tuple[Path, Path]:
    """Create a repo_root and a work_dir under it, plus a stub prompt.

    Returns ``(work_dir, repo_root)``. ``repo_root`` is ``tmp_path``;
    ``work_dir`` is ``tmp_path/work`` so ``work_dir.relative_to(repo_root)``
    is well-defined for the orchestrator's CWD-and-env wiring.
    """
    repo_root = tmp_path
    work_dir = repo_root / "work"
    (work_dir / "inputs" / "prompts").mkdir(parents=True)
    (work_dir / "outputs").mkdir(parents=True)
    (work_dir / "inputs" / "prompts" / f"{phase}.md").write_text("stub prompt")
    return work_dir, repo_root


class _FakePopen:
    """Minimal Popen stand-in supporting context manager + line iteration."""

    def __init__(self, returncode: int, stdout_text: str) -> None:
        self.returncode = returncode
        self.stdout = iter(stdout_text.splitlines(keepends=True))
        self.stderr: Iterator[str] = iter([])

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
    work_dir, repo_root = _setup_work_dir(tmp_path, phase)
    side_effect, state, captured = _fake_pi_factory(work_dir, expected_rel, write_on_calls={1})

    run_mock = tripwire.mock.object(invoke_pi_mod.subprocess, "Popen")
    run_mock.calls(side_effect)

    with tripwire:
        events = invoke_pi_phase_with_retry(phase, work_dir, repo_root)

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
    work_dir, repo_root = _setup_work_dir(tmp_path, phase)
    # Only the second call writes the file.
    side_effect, state, captured = _fake_pi_factory(work_dir, expected_rel, write_on_calls={2})

    run_mock = tripwire.mock.object(invoke_pi_mod.subprocess, "Popen")
    run_mock.calls(side_effect).calls(side_effect)

    with tripwire:
        events = invoke_pi_phase_with_retry(phase, work_dir, repo_root)

    # Returned events are concatenated across both invocations so
    # summarize_usage can charge the caller for tokens consumed by the
    # failed first attempt AND the retry. Each fake invocation emits one
    # event line; combined we expect two copies.
    assert events == [_EVENT_PARSED, _EVENT_PARSED]
    assert state["calls"] == 2
    assert len(captured) == 2

    first_cmd = captured[0][0][0]
    second_cmd = captured[1][0][0]
    first_prompt = first_cmd[first_cmd.index("-p") + 1]
    second_prompt = second_cmd[second_cmd.index("-p") + 1]

    assert first_prompt == "stub prompt"
    # The reminder cites the path the model must pass to write_output,
    # which is relative to pi's CWD (= repo_root): work_dir_rel / expected_rel.
    expected_rel_to_cwd = str(work_dir.relative_to(repo_root) / expected_rel)
    expected_suffix = (
        "\n\n---\n"
        "CRITICAL REMINDER: You MUST invoke the `write_output` tool to write "
        f"`{expected_rel_to_cwd}`. Your previous attempt ended without writing "
        "this file. Do NOT respond with prose only; call `write_output` "
        "before ending your turn."
    )
    assert second_prompt == "stub prompt" + expected_suffix

    for args, kwargs in captured:
        run_mock.assert_call(args=args, kwargs=kwargs)


def test_retry_also_misses_file_raises(tmp_path: Path) -> None:
    phase = "phase2"
    expected_rel = PHASE_EXPECTED_OUTPUTS[phase]
    work_dir, repo_root = _setup_work_dir(tmp_path, phase)
    # Neither call writes the file.
    side_effect, state, captured = _fake_pi_factory(work_dir, expected_rel, write_on_calls=set())

    run_mock = tripwire.mock.object(invoke_pi_mod.subprocess, "Popen")
    run_mock.calls(side_effect).calls(side_effect)

    with tripwire, pytest.raises(PiInvocationError) as exc_info:
        invoke_pi_phase_with_retry(phase, work_dir, repo_root)

    assert state["calls"] == 2
    assert len(captured) == 2
    msg = str(exc_info.value)
    # Line 1 is what the PR rollup comment shows; the per-attempt detail
    # below it is what the Actions log shows.
    assert msg.splitlines()[0] == (
        f"phase {phase} did not produce expected output {expected_rel} after retry"
    )
    assert "attempt 1: turns=0 stopReason=<none> write_output calls=0" in msg
    assert "attempt 2: turns=0 stopReason=<none> write_output calls=0" in msg

    for args, kwargs in captured:
        run_mock.assert_call(args=args, kwargs=kwargs)


def test_first_call_nonzero_exit_raises_no_retry(tmp_path: Path) -> None:
    phase = "phase2"
    expected_rel = PHASE_EXPECTED_OUTPUTS[phase]
    work_dir, repo_root = _setup_work_dir(tmp_path, phase)
    side_effect, state, captured = _fake_pi_factory(
        work_dir, expected_rel, write_on_calls=set(), returncode=2
    )

    run_mock = tripwire.mock.object(invoke_pi_mod.subprocess, "Popen")
    run_mock.calls(side_effect)

    with tripwire, pytest.raises(PiInvocationError) as exc_info:
        invoke_pi_phase_with_retry(phase, work_dir, repo_root)

    # Pre-existing behavior: non-zero exit raises immediately, no retry.
    assert state["calls"] == 1
    assert len(captured) == 1
    msg = str(exc_info.value)
    assert f"phase {phase} failed with exit 2" in msg

    args, kwargs = captured[0]
    run_mock.assert_call(args=args, kwargs=kwargs)


def test_provider_error_in_message_end_raises(tmp_path: Path) -> None:
    """
    Pi exits 0 with a stopReason=error message when the provider rejects the
    request (e.g. 401). Without explicit detection this looked like an
    "empty turn" and triggered the missing-output retry, masking the real
    failure. The orchestrator should now raise immediately.
    """
    phase = "phase2"
    work_dir, repo_root = _setup_work_dir(tmp_path, phase)

    error_stream = (
        '{"type": "session"}\n'
        '{"type": "agent_start"}\n'
        '{"type": "turn_start"}\n'
        '{"type": "message_start", "message": {"role": "user"}}\n'
        '{"type": "message_end", "message": {"role": "user"}}\n'
        '{"type": "message_start", "message": {"role": "assistant"}}\n'
        '{"type": "message_end", "message": {"role": "assistant", '
        '"stopReason": "error", "errorMessage": "401 Missing Authentication header"}}\n'
        '{"type": "turn_end"}\n'
        '{"type": "agent_end"}\n'
    )

    state = {"calls": 0}
    captured: list[tuple[tuple, dict]] = []

    def side_effect(*args, **kwargs):
        state["calls"] += 1
        captured.append((args, kwargs))
        return _FakePopen(returncode=0, stdout_text=error_stream)

    run_mock = tripwire.mock.object(invoke_pi_mod.subprocess, "Popen")
    run_mock.calls(side_effect)

    with tripwire, pytest.raises(PiInvocationError) as exc_info:
        invoke_pi_phase_with_retry(phase, work_dir, repo_root)

    # Single call — no retry on provider error.
    assert state["calls"] == 1
    msg = str(exc_info.value)
    assert "provider error" in msg
    assert "401 Missing Authentication header" in msg

    args, kwargs = captured[0]
    run_mock.assert_call(args=args, kwargs=kwargs)


def test_phase_not_in_expected_outputs_skips_guard(tmp_path: Path) -> None:
    """invoke_pi_phase (the unguarded variant) does not check files."""
    phase = "phase2"
    work_dir, repo_root = _setup_work_dir(tmp_path, phase)
    # No file is ever written, but the unguarded function just returns events.
    side_effect, state, captured = _fake_pi_factory(
        work_dir, expected_rel_path=None, write_on_calls=set()
    )

    run_mock = tripwire.mock.object(invoke_pi_mod.subprocess, "Popen")
    run_mock.calls(side_effect)

    with tripwire:
        events = invoke_pi_phase(phase, work_dir, repo_root)

    assert events == [_EVENT_PARSED]
    assert state["calls"] == 1
    assert len(captured) == 1

    args, kwargs = captured[0]
    run_mock.assert_call(args=args, kwargs=kwargs)


def test_pi_spawned_with_cwd_repo_root_and_workdir_env(tmp_path: Path) -> None:
    """Pi must run with cwd=repo_root (so its built-in tools resolve repo
    files via plain relative paths) and MOMUS_WORK_DIR pointing at the
    work_dir relative to repo_root (so the readonly-tools extension's
    write_output knows the allowed prefix). Regression for the
    ``.momus/<repo-path>`` ENOENT bug.
    """
    phase = "phase2"
    expected_rel = PHASE_EXPECTED_OUTPUTS[phase]
    work_dir, repo_root = _setup_work_dir(tmp_path, phase)
    side_effect, state, captured = _fake_pi_factory(work_dir, expected_rel, write_on_calls={1})

    run_mock = tripwire.mock.object(invoke_pi_mod.subprocess, "Popen")
    run_mock.calls(side_effect)

    with tripwire:
        invoke_pi_phase_with_retry(phase, work_dir, repo_root)

    assert state["calls"] == 1
    args, kwargs = captured[0]
    assert kwargs["cwd"] == repo_root
    env = kwargs["env"]
    assert env["MOMUS_WORK_DIR"] == str(work_dir.relative_to(repo_root))

    run_mock.assert_call(args=args, kwargs=kwargs)


def test_invoke_pi_phase_rejects_work_dir_outside_repo_root(tmp_path: Path) -> None:
    """work_dir must live under repo_root; a sibling path makes
    work_dir.relative_to(repo_root) raise inside _build_pi_env. The
    orchestrator validates this earlier with a clearer error, but
    invoke_pi_phase running with a misconfigured pair must not silently
    succeed and re-introduce the CWD/path-resolution split.
    """
    phase = "phase2"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    work_dir = tmp_path / "work-elsewhere"
    (work_dir / "inputs" / "prompts").mkdir(parents=True)
    (work_dir / "inputs" / "prompts" / f"{phase}.md").write_text("stub prompt")
    (work_dir / "outputs").mkdir()

    # No tripwire mock: ValueError is expected to fire before subprocess
    # spawn, so any installed mock would be unused and the harness would
    # fail with UnusedMocksError instead of the assertion we care about.
    with pytest.raises(ValueError):
        invoke_pi_phase(phase, work_dir, repo_root)


def test_phase_expected_outputs_mapping_is_exactly_three_phases() -> None:
    assert PHASE_EXPECTED_OUTPUTS == {
        "phase1": "outputs/prior-findings.json",
        "phase2": "outputs/findings.json",
        "phase3": "outputs/findings.json",
    }


def _capture_log(event: dict, capsys: pytest.CaptureFixture[str]) -> str:
    """Run _log_event for a single event and return its stderr output."""
    capsys.readouterr()
    invoke_pi_mod._log_event("phase2", event, json.dumps(event))
    return capsys.readouterr().err


def test_log_event_suppresses_streaming_deltas(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # text_delta, toolcall_delta, *_start, message_start/end are all noise.
    noisy_events: list[dict] = [
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Hi"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_start"}},
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "toolcall_delta", "delta": "x"},
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "thinking_delta", "delta": "x"},
        },
        {"type": "message_update", "assistantMessageEvent": {"type": "done", "reason": "stop"}},
        {"type": "message_start"},
        {"type": "message_end"},
        {"type": "tool_execution_update", "toolName": "bash"},
    ]
    for noisy in noisy_events:
        assert _capture_log(noisy, capsys) == "", f"should suppress: {noisy}"


def test_log_event_emits_assembled_assistant_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    event = {
        "type": "message_update",
        "assistantMessageEvent": {"type": "text_end", "content": "Hello world"},
    }
    out = _capture_log(event, capsys)
    assert "[momus.pi phase2] assistant: Hello world" in out


def test_log_event_emits_tool_call_with_args(
    capsys: pytest.CaptureFixture[str],
) -> None:
    event = {
        "type": "message_update",
        "assistantMessageEvent": {
            "type": "toolcall_end",
            "toolCall": {"name": "read", "arguments": {"path": "src/main.py"}},
        },
    }
    out = _capture_log(event, capsys)
    assert "[momus.pi phase2] tool_call read: path=src/main.py" in out


def test_log_event_emits_tool_execution_start_and_end(
    capsys: pytest.CaptureFixture[str],
) -> None:
    start = {
        "type": "tool_execution_start",
        "toolName": "bash",
        "args": {"command": "ls -la"},
    }
    end = {
        "type": "tool_execution_end",
        "toolName": "bash",
        "isError": False,
        "result": {"content": [{"type": "text", "text": "total 4\nfile.txt"}]},
    }
    assert "[momus.pi phase2] running bash: command=ls -la" in _capture_log(start, capsys)
    assert "[momus.pi phase2] tool_result bash: total 4 \\n file.txt" in _capture_log(end, capsys)


def test_log_event_marks_tool_errors(capsys: pytest.CaptureFixture[str]) -> None:
    event = {
        "type": "tool_execution_end",
        "toolName": "bash",
        "isError": True,
        "result": {"content": [{"type": "text", "text": "exit 1: not found"}]},
    }
    out = _capture_log(event, capsys)
    assert "[momus.pi phase2] tool_error bash:" in out


def test_log_event_lifecycle_events_pass_through(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _capture_log({"type": "agent_start", "model": "deepseek/v4"}, capsys)
    assert "[momus.pi phase2] agent_start model=deepseek/v4" in out
    out = _capture_log({"type": "compaction_start", "reason": "threshold"}, capsys)
    assert "[momus.pi phase2] compaction_start reason=threshold" in out


def test_render_phase_prompt_called_with_work_dir_for_phase1(monkeypatch, tmp_path: Path) -> None:
    """Phase 1 invocations of ``render_phase_prompt`` must pass the
    absolute ``work_dir`` as a kwarg so the path-loaded
    ``<<UNTRUSTED_PRIOR_THREADS_JSON>>`` placeholder can fence the file
    contents. Other phases pass ``work_dir=None`` (default).
    """
    from momus import prep as prep_mod
    from momus.config import load_config
    from momus.prep import prep_inputs

    captured: list[dict] = []

    def fake_render(phase, config, run_id, work_dir_rel, work_dir=None):
        captured.append(
            {
                "phase": phase,
                "run_id": run_id,
                "work_dir_rel": work_dir_rel,
                "work_dir": work_dir,
            }
        )
        return f"rendered:{phase}"

    monkeypatch.setattr(prep_mod, "render_phase_prompt", fake_render)

    # Stub git invocations: prep_inputs shells out to `git diff`.
    class _Proc:
        def __init__(self, stdout: str = "") -> None:
            self.stdout = stdout

    def fake_run(cmd, **kwargs):
        return _Proc(stdout="")

    monkeypatch.setattr(prep_mod.subprocess, "run", fake_run)

    repo_root = tmp_path
    work_dir = repo_root / ".work"
    work_dir.mkdir()
    work_dir_rel = work_dir.relative_to(repo_root)

    pr_meta = {
        "base_sha": "deadbeef",
        "head_sha": "cafebabe",
        "run_id": "A",
        "pr_number": 1,
        "owner": "x",
        "repo": "y",
    }
    config = load_config(repo_root)

    prep_inputs(repo_root, work_dir, work_dir_rel, pr_meta, config)

    expected_phases = ["phase1", "phase2", "phase3"]
    assert [c["phase"] for c in captured] == expected_phases
    by_phase = {c["phase"]: c for c in captured}
    assert by_phase["phase1"]["work_dir"] == work_dir
    assert by_phase["phase2"]["work_dir"] is None
    assert by_phase["phase3"]["work_dir"] is None
    assert by_phase["phase1"]["work_dir_rel"] == work_dir_rel
    assert by_phase["phase1"]["run_id"] == "A"


# ---------------------------------------------------------------------------
# W2-AllowlistSwap: Phase tool allowlists use *_repo variants.
# ---------------------------------------------------------------------------


def test_phase_allowlists_use_repo_suffixed_tools():
    from momus.invoke_pi import PHASE_TOOL_ALLOWLISTS

    assert PHASE_TOOL_ALLOWLISTS["phase1"] == ["write_output"]
    for ph in ("phase2", "phase3"):
        assert PHASE_TOOL_ALLOWLISTS[ph] == [
            "read_repo",
            "grep_repo",
            "find_repo",
            "ls_repo",
            "bash_ro",
            "write_output",
        ]


def test_phase_allowlists_omit_pi_builtins():
    from momus.invoke_pi import PHASE_TOOL_ALLOWLISTS

    for ph in ("phase2", "phase3"):
        for forbidden in ("read", "grep", "find", "ls", "edit", "write", "bash"):
            assert forbidden not in PHASE_TOOL_ALLOWLISTS[ph]


# ---------------------------------------------------------------------------
# W3-EnvAllowlist: default-deny env passed to pi subprocess.
# ---------------------------------------------------------------------------


@pytest.fixture
def _scrubbed_env(monkeypatch):
    """Reset the ambient env to a known minimal state.

    Keeps only PATH/HOME/TMPDIR (needed by Python itself + tmp_path); every
    other key is removed so individual W3 tests can assert presence/absence
    of specific keys without ambient-noise interference.
    """
    keep = {"PATH", "HOME", "TMPDIR"}
    for k in list(os.environ.keys()):
        if k not in keep:
            monkeypatch.delenv(k, raising=False)


@pytest.mark.usefixtures("_scrubbed_env")
def test_pi_env_includes_path_home_lang(monkeypatch, tmp_path):
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path)
    assert env.get("PATH")
    assert env.get("HOME")
    assert env.get("LANG") == "en_US.UTF-8"


@pytest.mark.usefixtures("_scrubbed_env")
def test_pi_env_excludes_github_token(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path)
    assert "GITHUB_TOKEN" not in env


@pytest.mark.usefixtures("_scrubbed_env")
def test_pi_env_excludes_arbitrary_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_CUSTOM_SECRET", "shhh")
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path)
    assert "MY_CUSTOM_SECRET" not in env


@pytest.mark.usefixtures("_scrubbed_env")
def test_pi_env_passes_lc_glob(monkeypatch, tmp_path):
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("LC_CTYPE", "en_US.UTF-8")
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path)
    assert env.get("LC_ALL") == "C.UTF-8"
    assert env.get("LC_CTYPE") == "en_US.UTF-8"


@pytest.mark.usefixtures("_scrubbed_env")
def test_pi_env_passes_language(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGUAGE", "en_US")
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path)
    assert env.get("LANGUAGE") == "en_US"


@pytest.mark.usefixtures("_scrubbed_env")
def test_pi_env_includes_llm_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("LLM_MODEL", "model-x")
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path)
    assert env.get("LLM_API_KEY") == "k"
    assert env.get("LLM_BASE_URL") == "https://api.example.com"
    assert env.get("LLM_MODEL") == "model-x"


@pytest.mark.usefixtures("_scrubbed_env")
def test_pi_cmd_and_env_default_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    cmd = invoke_pi_mod._build_pi_command("prompt", ["read_repo"])
    assert "--model" in cmd
    model_idx = cmd.index("--model") + 1
    assert cmd[model_idx] == "z-ai/glm-5.2:free"
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path)
    assert env.get("LLM_BASE_URL") == "https://openrouter.ai/api/v1"
    assert env.get("LLM_MODEL") == "z-ai/glm-5.2:free"


@pytest.mark.usefixtures("_scrubbed_env")
def test_pi_env_passthrough_passes_listed_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("FOO", "x")
    monkeypatch.setenv("BAR", "y")
    monkeypatch.setenv("MOMUS_PI_ENV_PASSTHROUGH", "FOO,BAR")
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path)
    assert env.get("FOO") == "x"
    assert env.get("BAR") == "y"


@pytest.mark.usefixtures("_scrubbed_env")
def test_pi_env_passthrough_rejects_lowercase_keys(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("foo", "x")
    monkeypatch.setenv("MOMUS_PI_ENV_PASSTHROUGH", "foo")
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    with caplog.at_level(logging.INFO):
        env = invoke_pi_mod._build_pi_env(work_dir, tmp_path)
    assert "foo" not in env
    # D3: skipped passthrough keys are logged at INFO level.
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "pi_env_passthrough_skipped_invalid_key" in r.getMessage() or "foo" in r.getMessage()
        for r in info_records
    )


@pytest.mark.usefixtures("_scrubbed_env")
def test_pi_env_passthrough_skips_digit_leading(monkeypatch, tmp_path):
    monkeypatch.setenv("1FOO", "x")
    monkeypatch.setenv("MOMUS_PI_ENV_PASSTHROUGH", "1FOO")
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path)
    assert "1FOO" not in env


@pytest.mark.usefixtures("_scrubbed_env")
def test_pi_env_passthrough_skips_key_with_space(monkeypatch, tmp_path):
    monkeypatch.setenv("MOMUS_PI_ENV_PASSTHROUGH", "FOO BAR")
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path)
    assert "FOO BAR" not in env
    assert "FOO" not in env
    assert "BAR" not in env


@pytest.mark.usefixtures("_scrubbed_env")
def test_pi_env_passthrough_skips_keys_not_set_on_parent(monkeypatch, tmp_path):
    # MOMUS_PI_ENV_PASSTHROUGH lists FOO, but FOO is not set in parent env.
    # No KeyError; FOO simply not added.
    monkeypatch.setenv("MOMUS_PI_ENV_PASSTHROUGH", "FOO")
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path)
    assert "FOO" not in env


@pytest.mark.usefixtures("_scrubbed_env")
def test_pi_env_passthrough_does_not_override_momus_work_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("MOMUS_WORK_DIR", "/should/not/win")
    monkeypatch.setenv("MOMUS_PI_ENV_PASSTHROUGH", "MOMUS_WORK_DIR")
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path)
    # Orchestrator's value wins; passthrough listing is reserved-skipped.
    assert env["MOMUS_WORK_DIR"] == "w"


@pytest.mark.usefixtures("_scrubbed_env")
def test_pi_env_passthrough_skips_reserved_toolcall_log(monkeypatch, tmp_path):
    monkeypatch.setenv("MOMUS_TOOLCALL_LOG", "/tmp/tc.jsonl")
    monkeypatch.setenv("MOMUS_PI_ENV_PASSTHROUGH", "MOMUS_TOOLCALL_LOG")
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path)
    # MOMUS_TOOLCALL_LOG is _RESERVED_PASSTHROUGH and must not be set via
    # the passthrough mechanism (orchestrator/harness-only).
    assert "MOMUS_TOOLCALL_LOG" not in env


@pytest.mark.usefixtures("_scrubbed_env")
def test_pi_env_sets_momus_work_dir_relative(monkeypatch, tmp_path):
    work_dir = tmp_path / "nested" / "wd"
    work_dir.mkdir(parents=True)
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path)
    assert env["MOMUS_WORK_DIR"] == str(Path("nested") / "wd")


@pytest.mark.usefixtures("_scrubbed_env")
def test_pi_env_does_not_mutate_os_environ(monkeypatch, tmp_path):
    snapshot = dict(os.environ)
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    invoke_pi_mod._build_pi_env(work_dir, tmp_path)
    assert dict(os.environ) == snapshot


# ---------------------------------------------------------------------------
# summarize_usage: cost + token totals from pi event stream
# ---------------------------------------------------------------------------


def _turn_end(input_t: int, output_t: int, total_cost: float) -> dict:
    """Build a turn_end event the way pi-ai emits it: usage on the
    assistant message, with cost.total computed by pi-ai's calculateCost.
    """
    return {
        "type": "turn_end",
        "message": {
            "usage": {
                "input": input_t,
                "output": output_t,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": input_t + output_t,
                "cost": {
                    "input": 0.0,
                    "output": 0.0,
                    "cacheRead": 0.0,
                    "cacheWrite": 0.0,
                    "total": total_cost,
                },
            },
        },
    }


def test_summarize_usage_sums_turn_end_costs(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "deepseek/deepseek-v4-pro")
    events = [
        {"type": "turn_start"},
        _turn_end(100, 20, 0.001),
        {"type": "tool_execution_end"},
        _turn_end(200, 50, 0.0025),
    ]
    u = summarize_usage(events)
    assert u == PhaseUsage(
        cost_usd=0.0035,
        input_tokens=300,
        output_tokens=70,
        cached_tokens=0,
        model="deepseek/deepseek-v4-pro",
    )


def test_summarize_usage_ignores_message_end_to_avoid_double_counting(
    monkeypatch,
):
    # Pi emits message_end and turn_end with the same usage block at the
    # close of every turn. summarize_usage must only count one of them so
    # the total isn't doubled.
    monkeypatch.setenv("LLM_MODEL", "deepseek/deepseek-v4-pro")
    paired = _turn_end(100, 20, 0.001)
    message_end = {**paired, "type": "message_end"}
    events = [message_end, paired]
    u = summarize_usage(events)
    assert u.cost_usd == 0.001
    assert u.input_tokens == 100


def test_summarize_usage_zero_when_no_turn_end_events(monkeypatch):
    # Phase aborted before the first turn (e.g. provider rate limit on
    # the very first request). No usage is emitted; totals stay at zero.
    monkeypatch.setenv("LLM_MODEL", "deepseek/deepseek-v4-pro")
    events = [{"type": "turn_start"}, {"type": "agent_end"}]
    u = summarize_usage(events)
    assert u.cost_usd == 0.0
    assert u.input_tokens == 0
    assert u.output_tokens == 0


def test_summarize_usage_handles_missing_cost_fields(monkeypatch):
    # Defensive: an event with usage but no cost subobject (e.g. a model
    # whose cost was zeroed at provider registration) reports tokens but
    # zero cost rather than KeyError.
    monkeypatch.setenv("LLM_MODEL", "unknown-model")
    events = [
        {
            "type": "turn_end",
            "message": {
                "usage": {"input": 50, "output": 10, "totalTokens": 60},
            },
        },
    ]
    u = summarize_usage(events)
    assert u.cost_usd == 0.0
    assert u.input_tokens == 50
    assert u.output_tokens == 10
    assert u.model == "unknown-model"


# --- Output-token-cap truncation (phase2 crash on axiomantic/spellbook) -----
#
# Observed three times: pi exits 0, the final assistant message carries
# `stopReason: "length"` because the model burned its whole output budget on
# reasoning tokens, and it therefore never emitted the `write_output` call.
# The old guard reported only "did not produce expected output ... after
# retry", which is true of every failure mode and diagnoses none of them.

_LENGTH_STOP_STREAM = (
    '{"type": "agent_start"}\n'
    '{"type": "turn_start"}\n'
    '{"type": "message_update", "assistantMessageEvent": '
    '{"type": "thinking_end", "content": "MEDIUM: publish.py drops the redaction on retry"}}\n'
    '{"type": "message_end", "message": {"role": "assistant", "stopReason": "length"}}\n'
    '{"type": "turn_end"}\n'
    '{"type": "agent_end"}\n'
)


def _length_stop_factory() -> tuple:
    state = {"calls": 0}
    captured: list[tuple[tuple, dict]] = []

    def side_effect(*args, **kwargs):
        state["calls"] += 1
        captured.append((args, kwargs))
        return _FakePopen(returncode=0, stdout_text=_LENGTH_STOP_STREAM)

    return side_effect, state, captured


def test_length_stop_names_the_output_cap_in_first_line(tmp_path: Path) -> None:
    """A `length` stop must be named on line 1 of the error.

    `__main__.main` surfaces only the first line of the exception to the PR
    rollup comment, so a diagnosis buried on a later line never reaches the
    person reading the failure.
    """
    phase = "phase2"
    work_dir, repo_root = _setup_work_dir(tmp_path, phase)
    side_effect, state, captured = _length_stop_factory()

    run_mock = tripwire.mock.object(invoke_pi_mod.subprocess, "Popen")
    run_mock.calls(side_effect).calls(side_effect)

    with tripwire, pytest.raises(PiInvocationError) as exc_info:
        invoke_pi_phase_with_retry(phase, work_dir, repo_root)

    assert state["calls"] == 2
    first_line = str(exc_info.value).splitlines()[0]
    assert "stopReason=length" in first_line
    assert "MOMUS_PI_MAX_TOKENS" in first_line
    # The rollup comment truncates at 240 chars; the diagnosis must fit.
    assert len(first_line) <= 240

    for args, kwargs in captured:
        run_mock.assert_call(args=args, kwargs=kwargs)


def test_length_stop_reports_per_attempt_detail(tmp_path: Path) -> None:
    phase = "phase2"
    work_dir, repo_root = _setup_work_dir(tmp_path, phase)
    side_effect, _state, captured = _length_stop_factory()

    run_mock = tripwire.mock.object(invoke_pi_mod.subprocess, "Popen")
    run_mock.calls(side_effect).calls(side_effect)

    with tripwire, pytest.raises(PiInvocationError) as exc_info:
        invoke_pi_phase_with_retry(phase, work_dir, repo_root)

    msg = str(exc_info.value)
    assert "attempt 1:" in msg
    assert "attempt 2:" in msg
    # write_output was never called in either attempt; say so, because a
    # partial write is a different failure with a different remedy.
    assert "write_output calls=0" in msg

    for args, kwargs in captured:
        run_mock.assert_call(args=args, kwargs=kwargs)


def test_missing_output_salvages_last_assistant_message_per_attempt(
    tmp_path: Path,
) -> None:
    """The last assistant message is written under outputs/ for salvage.

    outputs/ is uploaded as a workflow artifact even on failure, so a crash
    that loses findings.json still leaves the reasoning that produced the
    findings recoverable without hand-mining a truncated Actions log.
    """
    phase = "phase2"
    work_dir, repo_root = _setup_work_dir(tmp_path, phase)
    side_effect, _state, captured = _length_stop_factory()

    run_mock = tripwire.mock.object(invoke_pi_mod.subprocess, "Popen")
    run_mock.calls(side_effect).calls(side_effect)

    with tripwire, pytest.raises(PiInvocationError):
        invoke_pi_phase_with_retry(phase, work_dir, repo_root)

    for attempt in (1, 2):
        salvage = work_dir / "outputs" / f"{phase}-attempt{attempt}-last-message.txt"
        assert salvage.exists(), f"attempt {attempt} salvage file missing"
        body = salvage.read_text(encoding="utf-8")
        # Full text, not the 300-char log snippet.
        assert "MEDIUM: publish.py drops the redaction on retry" in body
        assert "stopReason: length" in body

    for args, kwargs in captured:
        run_mock.assert_call(args=args, kwargs=kwargs)


def test_generic_missing_output_still_reported_when_stop_reason_is_normal(
    tmp_path: Path,
) -> None:
    """A plain missing-output failure keeps its original first line."""
    phase = "phase2"
    expected_rel = PHASE_EXPECTED_OUTPUTS[phase]
    work_dir, repo_root = _setup_work_dir(tmp_path, phase)
    side_effect, _state, captured = _fake_pi_factory(work_dir, expected_rel, write_on_calls=set())

    run_mock = tripwire.mock.object(invoke_pi_mod.subprocess, "Popen")
    run_mock.calls(side_effect).calls(side_effect)

    with tripwire, pytest.raises(PiInvocationError) as exc_info:
        invoke_pi_phase_with_retry(phase, work_dir, repo_root)

    first_line = str(exc_info.value).splitlines()[0]
    assert first_line == (
        f"phase {phase} did not produce expected output {expected_rel} after retry"
    )

    for args, kwargs in captured:
        run_mock.assert_call(args=args, kwargs=kwargs)


def test_log_event_emits_assistant_stop_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """stopReason is the field that identifies a truncated turn.

    Suppressing every message_end is what made the three observed crashes
    undiagnosable from the Actions log.
    """
    event = {
        "type": "message_end",
        "message": {"role": "assistant", "stopReason": "length"},
    }
    assert "[momus.pi phase2] message_end stopReason=length" in _capture_log(event, capsys)


def test_log_event_still_suppresses_uninteresting_message_end(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for quiet in (
        {"type": "message_end"},
        {"type": "message_end", "message": {"role": "user"}},
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "toolUse"}},
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}},
    ):
        assert _capture_log(quiet, capsys) == "", f"should suppress: {quiet}"


def test_emit_snippet_width_is_configurable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MOMUS_LOG_SNIPPET_CHARS", "20")
    capsys.readouterr()
    invoke_pi_mod._emit("phase2", "thinking", "x" * 500)
    out = capsys.readouterr().err
    assert "x" * 17 + "..." in out
    assert "x" * 21 not in out


def test_emit_snippet_width_defaults_to_300(capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    invoke_pi_mod._emit("phase2", "thinking", "x" * 500)
    out = capsys.readouterr().err
    assert "x" * 297 + "..." in out


def test_pi_env_forwards_max_tokens(monkeypatch, tmp_path):
    """The extension reads MOMUS_PI_MAX_TOKENS to size the provider's cap.

    The pi env is default-deny, so a key the extension needs must be on the
    allowlist or it silently does not arrive.
    """
    monkeypatch.setenv("MOMUS_PI_MAX_TOKENS", "32768")
    work_dir = tmp_path / "repo" / "work"
    work_dir.mkdir(parents=True)
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path / "repo")
    assert env["MOMUS_PI_MAX_TOKENS"] == "32768"


def test_log_event_marks_tool_error_reported_on_the_result_object(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pi puts isError on the result, not on the event.

    Observed: a `spawn rg ENOENT` from bash_ro was logged as `tool_result`,
    so a reviewer that had lost its search tool looked like one that had
    used it successfully.
    """
    event = {
        "type": "tool_execution_end",
        "toolName": "bash_ro",
        "result": {
            "isError": True,
            "content": [{"type": "text", "text": "spawn error: spawn rg ENOENT"}],
        },
    }
    out = _capture_log(event, capsys)
    assert "[momus.pi phase2] tool_error bash_ro: spawn error: spawn rg ENOENT" in out


# ---------------------------------------------------------------------------
# MOMUS_EXCLUDE_PATHS: crossing the Python-to-pi boundary
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_scrubbed_env")
def test_exclude_paths_cross_the_boundary_newline_separated(tmp_path):
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path, ["dist/", "!dist/keep.js"])
    assert env["MOMUS_EXCLUDE_PATHS"] == "dist/\n!dist/keep.js"


@pytest.mark.usefixtures("_scrubbed_env")
def test_exclude_paths_key_is_always_written_even_when_empty(tmp_path):
    """Leaving the key unset would let a stale parent value decide what
    the tool layer refuses; an empty string reads as 'exclude nothing'.
    """
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path, [])
    assert env["MOMUS_EXCLUDE_PATHS"] == ""
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path)
    assert env["MOMUS_EXCLUDE_PATHS"] == ""


@pytest.mark.usefixtures("_scrubbed_env")
def test_exclude_paths_is_on_the_allowlist_and_reserved():
    """Allowlisted so it survives the default-deny env; reserved so
    MOMUS_PI_ENV_PASSTHROUGH cannot forge it.
    """
    assert "MOMUS_EXCLUDE_PATHS" in invoke_pi_mod.PI_ENV_ALWAYS_ALLOW
    assert "MOMUS_EXCLUDE_PATHS" in invoke_pi_mod._RESERVED_PASSTHROUGH


@pytest.mark.usefixtures("_scrubbed_env")
def test_parent_env_cannot_forge_exclude_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("MOMUS_EXCLUDE_PATHS", "")
    monkeypatch.setenv("MOMUS_PI_ENV_PASSTHROUGH", "MOMUS_EXCLUDE_PATHS")
    work_dir = tmp_path / "w"
    work_dir.mkdir()
    env = invoke_pi_mod._build_pi_env(work_dir, tmp_path, ["dist/"])
    assert env["MOMUS_EXCLUDE_PATHS"] == "dist/"
