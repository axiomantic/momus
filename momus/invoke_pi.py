"""Invoke the pi CLI for one phase. Parses pi's --mode json event stream."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

EXTENSION_PATH = Path(__file__).resolve().parent / "extensions" / "readonly-tools.ts"

PHASE_TOOL_ALLOWLISTS: dict[str, list[str]] = {
    # Phase 1 needs no tools — input is a JSON file the model reads via the
    # prompt, output via write_output. Keep tools minimal.
    "phase1": ["write_output"],
    # Phase 2 needs the full read-only investigation toolkit.
    "phase2": ["read", "grep", "find", "ls", "bash_ro", "write_output"],
    # Phase 3 needs the same as phase 2 (audit by reading code at citations).
    "phase3": ["read", "grep", "find", "ls", "bash_ro", "write_output"],
}

# The "load-bearing" output each phase MUST produce. Phase 3 also writes
# audit-log.json, but findings.json is the one whose absence breaks the
# orchestrator. Phases not listed here skip the retry guard.
PHASE_EXPECTED_OUTPUTS: dict[str, str] = {
    "phase1": "outputs/prior-findings.json",
    "phase2": "outputs/findings.json",
    "phase3": "outputs/findings.json",
}


class PiInvocationError(RuntimeError):
    """Raised when pi exits non-zero or its event stream is unparseable."""


def invoke_pi_phase(
    phase: str,
    work_dir: Path,
    extra_prompt_suffix: str | None = None,
    on_tool_complete: Callable[[], None] | None = None,
) -> list[dict]:
    """
    Run a phase. ``work_dir`` is the CWD pi will see (must contain the
    repo checkout, plus inputs/ and outputs/ subdirs).

    Returns the list of parsed pi events from the JSON stream. The phase's
    real output is whatever it wrote to ``outputs/``.
    """
    prompt_path = work_dir / "inputs" / "prompts" / f"{phase}.md"
    if not prompt_path.exists():
        raise PiInvocationError(f"prompt not found: {prompt_path}")
    prompt = prompt_path.read_text(encoding="utf-8")
    if extra_prompt_suffix:
        prompt = prompt + extra_prompt_suffix

    tools = PHASE_TOOL_ALLOWLISTS[phase]
    cmd = _build_pi_command(prompt, tools)

    env = _build_pi_env()

    # Stream pi's output line-by-line so the orchestrator's stderr (and
    # therefore the GitHub Actions log) shows progress in real time. Pi's
    # stdout is a JSON-event stream; we parse each line AND echo a summary.
    print(
        f"[momus.pi] starting phase {phase} (tools={tools})",
        file=sys.stderr,
        flush=True,
    )
    start = time.monotonic()
    events: list[dict] = []
    stderr_tail: list[str] = []
    with subprocess.Popen(
        cmd,
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        bufsize=1,
    ) as proc:
        # Drain stdout line-by-line. stderr is drained at the end (it is
        # typically silent unless pi crashes). Reading stderr concurrently
        # would require threads; the simpler approach is fine for our case
        # because pi rarely fills its 64KB stderr pipe before exit.
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            event = _parse_one_line(line)
            events.append(event)
            _log_event(phase, event, line)
            # Within-phase progress signal: each completed tool execution
            # is one tick. Failures in the callback MUST NOT abort the
            # phase — progress reporting is best-effort.
            if on_tool_complete and event.get("type") == "tool_execution_end":
                try:
                    on_tool_complete()
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[momus.pi {phase}] progress callback error: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
        proc.wait()
        # Now safe to read stderr (process exited).
        if proc.stderr is not None:
            stderr_tail = [ln.rstrip("\n") for ln in proc.stderr]
            for ln in stderr_tail:
                print(f"[momus.pi:err] {ln}", file=sys.stderr, flush=True)

    elapsed = time.monotonic() - start
    print(
        f"[momus.pi] phase {phase} exited rc={proc.returncode} "
        f"after {elapsed:.1f}s ({len(events)} events)",
        file=sys.stderr,
        flush=True,
    )

    if proc.returncode != 0:
        last = events[-1] if events else {}
        raise PiInvocationError(
            f"phase {phase} failed with exit {proc.returncode}.\n"
            f"last event: {last}\n"
            f"stderr: {chr(10).join(stderr_tail).strip()}"
        )
    # Pi exits 0 even when the model itself returned an error (e.g. 401 from
    # the provider). The error string is buried inside an assistant message's
    # stopReason/errorMessage rather than a top-level error event, and the
    # output-existence retry guard in invoke_pi_phase_with_retry can't
    # distinguish "model returned no tool call" from "model never ran".
    # Surface it loudly here so a bad provider key fails fast instead of
    # masquerading as an empty turn.
    err_msg = _find_provider_error(events)
    if err_msg is not None:
        raise PiInvocationError(
            f"phase {phase} failed: provider error: {err_msg}"
        )
    return events


def _find_provider_error(events: list[dict]) -> str | None:
    """Scan event stream for an assistant message that ended with stopReason=error.

    Pi emits these on message_end / turn_end / agent_end with the error string
    in ``.message.errorMessage``. We only look at message-end events to keep
    the check cheap and unambiguous.
    """
    for ev in events:
        if ev.get("type") != "message_end":
            continue
        msg = ev.get("message") or {}
        if msg.get("stopReason") == "error":
            err = msg.get("errorMessage") or "<no errorMessage>"
            return str(err)
    return None


def _parse_one_line(line: str) -> dict:
    if not line.strip():
        return {"type": "_blank"}
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"type": "_unparsed", "line": line}


def _log_event(phase: str, event: dict, raw: str) -> None:
    """
    Emit a debuggable one-line summary for pi events to stderr.

    Pi streams token-level deltas inside `message_update` events; logging
    each one is pure noise. We only emit on completion boundaries — when
    a text block, tool call, or tool execution actually has content worth
    looking at — and on high-level lifecycle events.

    Schema reference: pi-mono coding-agent rpc.md.
    """
    et = event.get("type", "?")
    if et in ("_blank", "message_start", "message_end"):
        return
    if et == "_unparsed":
        print(f"[momus.pi {phase}] (raw) {raw[:300]}", file=sys.stderr, flush=True)
        return

    if et == "message_update":
        sub = event.get("assistantMessageEvent") or {}
        sub_type = sub.get("type", "")
        # text_end: assistant text block fully assembled. Emit the content.
        if sub_type == "text_end":
            content = sub.get("content") or ""
            _emit(phase, "assistant", content)
            return
        if sub_type == "thinking_end":
            content = sub.get("content") or ""
            _emit(phase, "thinking", content)
            return
        # toolcall_end: full tool call decided. Emit name + arg preview.
        if sub_type == "toolcall_end":
            tc = sub.get("toolCall") or {}
            name = tc.get("name", "?")
            args = tc.get("arguments") or tc.get("args") or {}
            _emit(phase, f"tool_call {name}", _summarize_args(args))
            return
        if sub_type == "error":
            reason = sub.get("reason", "?")
            print(
                f"[momus.pi {phase}] message error reason={reason}",
                file=sys.stderr,
                flush=True,
            )
            return
        # All other sub-types (start, *_delta, done) are streaming noise.
        return

    if et == "tool_execution_start":
        name = event.get("toolName", "?")
        args = event.get("args") or {}
        _emit(phase, f"running {name}", _summarize_args(args))
        return

    if et == "tool_execution_end":
        name = event.get("toolName", "?")
        is_error = bool(event.get("isError"))
        result = event.get("result") or {}
        text = _extract_result_text(result)
        label = f"tool_error {name}" if is_error else f"tool_result {name}"
        _emit(phase, label, text)
        return

    if et == "tool_execution_update":
        # Partial results — skip; tool_execution_end carries the full thing.
        return

    if et in ("session", "agent_start", "agent_end", "turn_start", "turn_end",
              "compaction_start", "compaction_end", "queue_update"):
        bits: list[str] = []
        for k in ("reason", "model", "messageCount"):
            if k in event and event[k] not in (None, ""):
                bits.append(f"{k}={event[k]}")
        suffix = (" " + " ".join(bits)) if bits else ""
        print(f"[momus.pi {phase}] {et}{suffix}", file=sys.stderr, flush=True)
        return

    # Unknown event type: print compactly so we notice it but don't bury logs.
    print(f"[momus.pi {phase}] {et}", file=sys.stderr, flush=True)


def _emit(phase: str, label: str, text: str) -> None:
    snippet = (text or "").strip().replace("\n", " \\n ")
    if not snippet:
        return
    if len(snippet) > 300:
        snippet = snippet[:297] + "..."
    print(f"[momus.pi {phase}] {label}: {snippet}", file=sys.stderr, flush=True)


def _summarize_args(args: dict) -> str:
    """Compact preview of tool args: key=value pairs, values truncated."""
    if not isinstance(args, dict) or not args:
        return ""
    parts: list[str] = []
    for k, v in args.items():
        if isinstance(v, str):
            sv = v.replace("\n", " ")
            if len(sv) > 120:
                sv = sv[:117] + "..."
        else:
            sv = json.dumps(v, separators=(",", ":"))
            if len(sv) > 120:
                sv = sv[:117] + "..."
        parts.append(f"{k}={sv}")
    return " ".join(parts)


def _extract_result_text(result: dict) -> str:
    """Pull a text snippet out of a tool result's content array."""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if isinstance(item, dict):
            t = item.get("text") or item.get("content")
            if isinstance(t, str):
                chunks.append(t)
    return "\n".join(chunks)


def invoke_pi_phase_with_retry(
    phase: str,
    work_dir: Path,
    on_tool_complete: Callable[[], None] | None = None,
) -> list[dict]:
    """
    Defense-in-depth wrapper around ``invoke_pi_phase``. If the phase exits
    0 but the expected output file is missing (model ended its turn without
    calling ``write_output``), retry once with a hard reminder appended to
    the prompt. Phases not in ``PHASE_EXPECTED_OUTPUTS`` skip the guard.
    """
    events = invoke_pi_phase(phase, work_dir, on_tool_complete=on_tool_complete)
    expected_rel = PHASE_EXPECTED_OUTPUTS.get(phase)
    if expected_rel is None:
        return events

    expected_path = work_dir / expected_rel
    if expected_path.exists():
        return events

    # The model likely returned prose instead of calling write_output. Nudge
    # it once with an explicit reminder; keep the suffix terse on purpose.
    suffix = (
        "\n\n---\n"
        "CRITICAL REMINDER: You MUST invoke the `write_output` tool to write "
        f"`{expected_rel}`. Your previous attempt ended without writing this "
        "file. Do NOT respond with prose only; call `write_output` before "
        "ending your turn."
    )
    events = invoke_pi_phase(
        phase,
        work_dir,
        extra_prompt_suffix=suffix,
        on_tool_complete=on_tool_complete,
    )
    if not expected_path.exists():
        raise PiInvocationError(
            f"phase {phase} did not produce expected output {expected_rel} "
            "after retry"
        )
    return events


def _build_pi_command(prompt: str, tools: list[str]) -> list[str]:
    cmd: list[str] = [
        "pi",
        "-p",
        prompt,
        "--mode",
        "json",
        "-e",
        str(EXTENSION_PATH),
        "--tools",
        ",".join(tools),
    ]
    # The pi CLI does NOT accept --base-url. Instead, the readonly-tools
    # extension calls pi.registerProvider("byo", ...) at load time using
    # LLM_BASE_URL / LLM_MODEL / LLM_API_KEY from the environment. We
    # reference that provider here by name. We still validate that the
    # env vars are set so the failure is surfaced from Python rather than
    # later inside pi's tool-call phase.
    base_url = os.environ.get("LLM_BASE_URL")
    model = os.environ.get("LLM_MODEL")
    if not base_url or not model:
        raise PiInvocationError(
            "LLM_BASE_URL and LLM_MODEL must be set in the environment "
            "(read by the readonly-tools extension to register the 'byo' "
            "provider)."
        )
    cmd += ["--provider", "byo", "--model", model]
    # LLM_API_KEY is forwarded via the environment (see _build_pi_env);
    # pi reads it by name because the extension registers
    # apiKey: "LLM_API_KEY". This keeps the secret out of argv.
    return cmd


def _build_pi_env() -> dict[str, str]:
    """Forward env to pi. The harness has LLM_API_KEY; pi consumes it."""
    env = dict(os.environ)
    return env


