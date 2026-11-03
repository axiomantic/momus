"""Invoke the pi CLI for one phase. Parses pi's --mode json event stream."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PhaseUsage:
    """Cost + token totals derived from a phase's pi event stream.

    Pi-ai writes `usage.cost.{input,output,cacheRead,cacheWrite,total}` on
    every assistant message using its bundled per-Mtok pricing table.
    Momus aggregates the per-turn totals (one `turn_end` event per round
    trip with the provider) and surfaces the sum in the review footer.

    `cost_usd` is in USD; sub-cent accuracy is preserved here and rounded
    only at render time. `model` is the configured `LLM_MODEL`, included
    for the footer line.
    """

    cost_usd: float
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    model: str


def summarize_usage(events: Iterable[dict]) -> PhaseUsage:
    """Sum per-turn cost + token usage from a pi event stream.

    Aggregates `event.message.usage` over `turn_end` events. Each turn
    corresponds to one provider request, so this gives a faithful total
    across the phase regardless of how many tool-calling round trips pi
    performed. `message_end` carries the same usage block; we count
    `turn_end` only to avoid double-counting.

    Returns zeros when the stream contains no usage data (e.g. phase
    aborted before any provider call). Callers decide how to render
    zero-cost cases.
    """
    cost = 0.0
    input_t = 0
    output_t = 0
    cached_t = 0
    model = os.environ.get("LLM_MODEL", "")
    for ev in events:
        if ev.get("type") != "turn_end":
            continue
        msg = ev.get("message") or {}
        usage = msg.get("usage") or {}
        cost_obj = usage.get("cost") or {}
        # Pi-ai's calculateCost emits .total as the sum of input+output+
        # cacheRead+cacheWrite costs. Use it directly to avoid drift if
        # the formula ever changes upstream.
        cost += float(cost_obj.get("total") or 0)
        input_t += int(usage.get("input") or 0)
        output_t += int(usage.get("output") or 0)
        cached_t += int(usage.get("cacheRead") or 0)
    return PhaseUsage(
        cost_usd=cost,
        input_tokens=input_t,
        output_tokens=output_t,
        cached_tokens=cached_t,
        model=model,
    )


EXTENSION_PATH = Path(__file__).resolve().parent / "extensions" / "readonly-tools.ts"

# W3: Default-deny env allowlist for the pi subprocess.
#
# Per design §W3 (refresh 2026-05-05): replace the previous wholesale
# `dict(os.environ)` passthrough with an explicit allowlist so that
# GITHUB_TOKEN, ACTIONS_RUNTIME_TOKEN, and arbitrary user secrets are
# unreachable from the LLM tool layer. Users with workflow-specific env
# vars can extend the allowlist via MOMUS_PI_ENV_PASSTHROUGH (comma-
# separated key names).
PI_ENV_ALWAYS_ALLOW: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LANGUAGE",
        "NODE_OPTIONS",
        "NODE_PATH",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_API_KEY",
        # Read by momus/extensions/readonly-tools.ts to size the "byo"
        # provider's per-message output cap. The default-deny env means a
        # key the extension needs but that is absent from this set does not
        # arrive and the extension silently falls back to its own default.
        "MOMUS_PI_MAX_TOKENS",
        # Newline-separated gitignore patterns from scope.exclude_paths.
        # The read tools refuse a matching path. Listed here because the
        # env is default-deny and the extension would otherwise never see
        # it; the orchestrator's value is written last (below) and always,
        # so nothing a caller set on the parent env can survive.
        "MOMUS_EXCLUDE_PATHS",
    }
)

DEFAULT_LLM_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_LLM_MODEL = "z-ai/glm-5.2:free"

# Width of the one-line event summaries written to stderr. The three
# observed phase-2 crashes were diagnosed only after the fact because the
# reasoning block that named the cause was clipped here; raise this when
# reproducing a failure.
LOG_SNIPPET_CHARS_ENV = "MOMUS_LOG_SNIPPET_CHARS"
DEFAULT_LOG_SNIPPET_CHARS = 300

# Any LC_* key passes (locale categories: LC_ALL, LC_CTYPE, LC_MESSAGES, ...).
PI_ENV_LC_PREFIX = "LC_"

# Conservative key-shape regex: uppercase-only, digit-or-underscore tail. The
# passthrough mechanism rejects anything that doesn't match — lowercase keys,
# digit-leading keys, keys with spaces or punctuation.
_PASSTHROUGH_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Keys reserved for orchestrator/harness use; even if listed in
# MOMUS_PI_ENV_PASSTHROUGH they are NOT forwarded (would otherwise let a
# malicious caller clobber MOMUS_WORK_DIR or redirect the toolcall log).
_RESERVED_PASSTHROUGH: frozenset[str] = frozenset(
    {
        "MOMUS_WORK_DIR",
        "MOMUS_PI_ENV_PASSTHROUGH",
        "MOMUS_TOOLCALL_LOG",
        # Forging this would let a caller shrink the tool layer's
        # exclusion list back to nothing.
        "MOMUS_EXCLUDE_PATHS",
    }
)

PHASE_TOOL_ALLOWLISTS: dict[str, list[str]] = {
    # Phase 1 needs no tools — input is a JSON file the model reads via the
    # prompt, output via write_output. Keep tools minimal.
    "phase1": ["write_output"],
    # Phase 2 needs the full read-only investigation toolkit.
    #
    # W2 substitution: the *_repo tools are cwd-contained replacements for
    # pi's filesystem-wide built-ins (read/grep/find/ls). The built-ins
    # remain registered inside pi but are not reachable from these phases
    # because they're absent from --tools. read_repo / grep_repo /
    # find_repo / ls_repo are registered by momus/extensions/readonly-
    # tools.ts and reject absolute / ~/ / ../-traversal paths plus
    # symlinks that escape cwd.
    "phase2": [
        "read_repo",
        "grep_repo",
        "find_repo",
        "ls_repo",
        "bash_ro",
        "write_output",
    ],
    # Phase 3 needs the same as phase 2 (audit by reading code at citations).
    "phase3": [
        "read_repo",
        "grep_repo",
        "find_repo",
        "ls_repo",
        "bash_ro",
        "write_output",
    ],
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
    repo_root: Path,
    extra_prompt_suffix: str | None = None,
    on_tool_complete: Callable[[], None] | None = None,
    exclude_paths: list[str] | None = None,
) -> list[dict]:
    """
    Run a phase. ``repo_root`` is pi's CWD: the repo checkout, where pi's
    built-in tools (read, ls, grep, find) and bash_ro resolve relative
    paths. ``work_dir`` is where inputs/ and outputs/ live; it MUST be a
    subdirectory of ``repo_root`` (validated by the orchestrator). Its
    location relative to repo_root is forwarded to the extension via
    ``MOMUS_WORK_DIR`` so write_output knows the allowed outputs prefix.

    ``exclude_paths`` is ``scope.exclude_paths``, forwarded as
    ``MOMUS_EXCLUDE_PATHS`` so the read tools refuse the same files the
    diff filter removed.

    Returns the list of parsed pi events from the JSON stream. The phase's
    real output is whatever it wrote under ``work_dir/outputs/``.
    """
    prompt_path = work_dir / "inputs" / "prompts" / f"{phase}.md"
    if not prompt_path.exists():
        raise PiInvocationError(f"prompt not found: {prompt_path}")
    prompt = prompt_path.read_text(encoding="utf-8")
    if extra_prompt_suffix:
        prompt = prompt + extra_prompt_suffix

    tools = PHASE_TOOL_ALLOWLISTS[phase]
    cmd = _build_pi_command(prompt, tools)

    env = _build_pi_env(work_dir, repo_root, exclude_paths)

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
        cwd=repo_root,
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
                except Exception as exc:
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
        raise PiInvocationError(f"phase {phase} failed: provider error: {err_msg}")
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


# A phase whose final assistant message stopped for this reason ran out of
# output tokens mid-message. Pi's agent loop sees a message with no tool
# call and treats the agent as finished, so pi exits 0 and the phase looks
# like a model that simply declined to write its output file.
TRUNCATING_STOP_REASON = "length"

# Stop reasons that are part of normal operation and not worth a log line.
_QUIET_STOP_REASONS: frozenset[str] = frozenset({"stop", "toolUse"})


@dataclass(frozen=True)
class PhaseOutcome:
    """What a phase's event stream says about how the phase ended.

    Built from the parsed stream so a missing-output failure can name its
    cause instead of reporting only that the file is absent, which is true
    of every failure mode and distinguishes none of them.
    """

    stop_reason: str | None
    n_turns: int
    n_write_output_calls: int
    last_assistant_text: str

    @property
    def truncated(self) -> bool:
        return self.stop_reason == TRUNCATING_STOP_REASON


def summarize_outcome(events: Iterable[dict]) -> PhaseOutcome:
    """Derive a PhaseOutcome from a pi event stream.

    ``stop_reason`` is taken from the LAST assistant ``message_end``: that is
    the message whose termination decided whether the agent loop continued,
    so it is the one that explains a phase that ended without its output.
    """
    stop_reason: str | None = None
    n_turns = 0
    n_write_output = 0
    # Blocks of the message currently being streamed; snapshotted into
    # `last_text` when that message ends, so the result is the final
    # message's own content rather than an accumulation across the phase.
    current: list[str] = []
    last_text = ""
    for ev in events:
        et = ev.get("type")
        if et == "turn_end":
            n_turns += 1
            continue
        if et == "message_end":
            msg = ev.get("message") or {}
            if msg.get("role") == "assistant":
                reason = msg.get("stopReason")
                stop_reason = str(reason) if reason is not None else None
                last_text = "\n\n".join(current)
                current = []
            continue
        if et == "message_update":
            sub = ev.get("assistantMessageEvent") or {}
            sub_type = sub.get("type")
            if sub_type in ("text_end", "thinking_end"):
                content = sub.get("content") or ""
                if content:
                    label = "assistant" if sub_type == "text_end" else "thinking"
                    current.append(f"[{label}]\n{content}")
            elif sub_type == "toolcall_end":
                tc = sub.get("toolCall") or {}
                if tc.get("name") == "write_output":
                    n_write_output += 1
            continue
    # A stream that ended without a closing assistant message_end (pi killed
    # mid-message) still has content worth keeping.
    if current and not last_text:
        last_text = "\n\n".join(current)
    return PhaseOutcome(
        stop_reason=stop_reason,
        n_turns=n_turns,
        n_write_output_calls=n_write_output,
        last_assistant_text=last_text,
    )


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
    if et in ("_blank", "message_start"):
        return
    if et == "message_end":
        # Only the assistant's terminal stop reason is worth a line, and
        # only when it is not the ordinary "stop"/"toolUse" pair. This is
        # the field that distinguishes a model which chose to stop from one
        # that was cut off at its output cap; suppressing every message_end
        # is what made the observed phase-2 crashes undiagnosable.
        msg = event.get("message") or {}
        reason = msg.get("stopReason")
        if msg.get("role") != "assistant" or not reason or reason in _QUIET_STOP_REASONS:
            return
        detail = f"[momus.pi {phase}] message_end stopReason={reason}"
        err = msg.get("errorMessage")
        if err:
            detail += f" errorMessage={err}"
        print(detail, file=sys.stderr, flush=True)
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
        result = event.get("result") or {}
        # Pi carries isError on the result object, not on the event. Reading
        # only the event mislabels every failed tool call as a success: an
        # observed `spawn rg ENOENT` logged as `tool_result`, not
        # `tool_error`. Accept either position.
        is_error = bool(event.get("isError") or result.get("isError"))
        text = _extract_result_text(result)
        label = f"tool_error {name}" if is_error else f"tool_result {name}"
        _emit(phase, label, text)
        return

    if et == "tool_execution_update":
        # Partial results — skip; tool_execution_end carries the full thing.
        return

    if et in (
        "session",
        "agent_start",
        "agent_end",
        "turn_start",
        "turn_end",
        "compaction_start",
        "compaction_end",
        "queue_update",
    ):
        bits: list[str] = [
            f"{k}={event[k]}"
            for k in ("reason", "model", "messageCount")
            if k in event and event[k] not in (None, "")
        ]
        suffix = (" " + " ".join(bits)) if bits else ""
        print(f"[momus.pi {phase}] {et}{suffix}", file=sys.stderr, flush=True)
        return

    # Unknown event type: print compactly so we notice it but don't bury logs.
    print(f"[momus.pi {phase}] {et}", file=sys.stderr, flush=True)


def _snippet_chars() -> int:
    """Width of the stderr event snippets, overridable per run."""
    raw = os.environ.get(LOG_SNIPPET_CHARS_ENV, "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_LOG_SNIPPET_CHARS
    return value if value > 0 else DEFAULT_LOG_SNIPPET_CHARS


def _emit(phase: str, label: str, text: str) -> None:
    snippet = (text or "").strip().replace("\n", " \\n ")
    if not snippet:
        return
    width = _snippet_chars()
    if len(snippet) > width:
        snippet = snippet[: max(width - 3, 1)] + "..."
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
    repo_root: Path,
    on_tool_complete: Callable[[], None] | None = None,
    exclude_paths: list[str] | None = None,
) -> list[dict]:
    """
    Defense-in-depth wrapper around ``invoke_pi_phase``. If the phase exits
    0 but the expected output file is missing (model ended its turn without
    calling ``write_output``), retry once with a hard reminder appended to
    the prompt. Phases not in ``PHASE_EXPECTED_OUTPUTS`` skip the guard.
    """
    events = invoke_pi_phase(
        phase,
        work_dir,
        repo_root,
        on_tool_complete=on_tool_complete,
        exclude_paths=exclude_paths,
    )
    expected_rel = PHASE_EXPECTED_OUTPUTS.get(phase)
    if expected_rel is None:
        return events

    expected_path = work_dir / expected_rel
    if expected_path.exists():
        return events

    first = summarize_outcome(events)
    _salvage_last_message(phase, 1, work_dir, first)

    # The model either returned prose instead of calling write_output, or was
    # cut off at its output cap before it got there. Nudge it once. When the
    # first attempt was truncated, an ordinary "you forgot" reminder makes
    # things worse (more to reason about inside the same budget), so tell it
    # to spend the budget on the file rather than on analysis.
    #
    # The reminder cites the path relative to pi's CWD (repo_root) so the
    # path the model must pass to write_output matches the path in the
    # reminder verbatim.
    work_dir_rel = work_dir.relative_to(repo_root)
    expected_rel_to_cwd = str(work_dir_rel / expected_rel)
    suffix = (
        "\n\n---\n"
        "CRITICAL REMINDER: You MUST invoke the `write_output` tool to write "
        f"`{expected_rel_to_cwd}`. Your previous attempt ended without writing "
        "this file. Do NOT respond with prose only; call `write_output` "
        "before ending your turn."
    )
    if first.truncated:
        suffix += (
            " Your previous attempt ran out of output tokens while reasoning "
            "and never reached the tool call. Keep analysis short, investigate "
            "no further than you must, and call `write_output` EARLY: a "
            "partial findings file is worth more than a complete analysis you "
            "never write down."
        )
    retry_events = invoke_pi_phase(
        phase,
        work_dir,
        repo_root,
        extra_prompt_suffix=suffix,
        on_tool_complete=on_tool_complete,
        exclude_paths=exclude_paths,
    )
    # Concatenate both invocations so summarize_usage charges the caller
    # for tokens consumed on the failed first attempt AND the retry.
    events = events + retry_events
    if not expected_path.exists():
        second = summarize_outcome(retry_events)
        _salvage_last_message(phase, 2, work_dir, second)
        raise PiInvocationError(_missing_output_message(phase, expected_rel, first, second))
    return events


def _missing_output_message(
    phase: str,
    expected_rel: str,
    first: PhaseOutcome,
    second: PhaseOutcome,
) -> str:
    """Build the missing-output error.

    ``__main__.main`` surfaces only the FIRST LINE of this message to the PR
    rollup comment (truncated at 240 chars), so any diagnosis that does not
    fit on line 1 never reaches the person reading the failure. Per-attempt
    detail goes on the lines below, which reach the Actions log.
    """
    headline = f"phase {phase} did not produce expected output {expected_rel} after retry"
    truncated = [n for n, o in ((1, first), (2, second)) if o.truncated]
    if truncated:
        which = "both attempts" if len(truncated) == 2 else f"attempt {truncated[0]}"
        headline += (
            f" ({which} ended with stopReason={TRUNCATING_STOP_REASON}: the model spent its "
            "whole per-message output budget before calling write_output; raise "
            "MOMUS_PI_MAX_TOKENS)"
        )
    lines = [headline]
    for n, outcome in ((1, first), (2, second)):
        lines.append(
            f"  attempt {n}: turns={outcome.n_turns} "
            f"stopReason={outcome.stop_reason or '<none>'} "
            f"write_output calls={outcome.n_write_output_calls}"
        )
    lines.append(
        f"  last assistant message of each attempt saved to "
        f"outputs/{phase}-attempt<N>-last-message.txt (uploaded as a run artifact)"
    )
    return "\n".join(lines)


def _salvage_last_message(
    phase: str,
    attempt: int,
    work_dir: Path,
    outcome: PhaseOutcome,
) -> None:
    """Persist a failed attempt's final assistant message under outputs/.

    outputs/ is uploaded as a workflow artifact even when the run fails, so
    whatever analysis the model completed before it died stays recoverable.
    Without this the only record is the stderr log, whose per-event snippets
    are clipped to `_snippet_chars()` and cannot be reassembled.

    Best-effort: a failure to write the salvage file must not replace the
    real error with an IO error.
    """
    if not outcome.last_assistant_text:
        return
    path = work_dir / "outputs" / f"{phase}-attempt{attempt}-last-message.txt"
    body = (
        f"phase: {phase}\n"
        f"attempt: {attempt}\n"
        f"stopReason: {outcome.stop_reason or '<none>'}\n"
        f"turns: {outcome.n_turns}\n"
        f"write_output calls: {outcome.n_write_output_calls}\n"
        f"---\n{outcome.last_assistant_text}\n"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        print(
            f"[momus.pi {phase}] could not save salvage file {path}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return
    print(
        f"[momus.pi {phase}] saved attempt {attempt} final message to {path}",
        file=sys.stderr,
        flush=True,
    )


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
    # reference that provider here by name.
    model = os.environ.get("LLM_MODEL") or DEFAULT_LLM_MODEL
    cmd += ["--provider", "byo", "--model", model]
    # LLM_API_KEY is forwarded via the environment (see _build_pi_env);
    # pi reads it by name because the extension registers
    # apiKey: "LLM_API_KEY". This keeps the secret out of argv.
    return cmd


def _build_pi_env(
    work_dir: Path,
    repo_root: Path,
    exclude_paths: list[str] | None = None,
) -> dict[str, str]:
    """Build the env handed to the pi subprocess (W3: default-deny).

    Returns a fresh dict containing only:

    1. Keys in ``PI_ENV_ALWAYS_ALLOW`` that are set on the parent.
    2. Any ``LC_*`` key (locale categories).
    3. Keys named in ``$MOMUS_PI_ENV_PASSTHROUGH`` (comma-separated) that
       (a) match the conservative shape ``^[A-Z][A-Z0-9_]*$`` and
       (b) are NOT in ``PI_ENV_ALWAYS_ALLOW``, the LC_ glob, or
       ``_RESERVED_PASSTHROUGH``. Skipped keys are logged at INFO (D3).
    4. ``MOMUS_WORK_DIR`` set to ``work_dir.relative_to(repo_root)`` and
       ``MOMUS_EXCLUDE_PATHS`` set to ``exclude_paths`` joined by
       newlines, both written LAST and unconditionally so neither a
       passthrough nor an allowlisted parent value can clobber the
       orchestrator's. An empty ``exclude_paths`` still writes an empty
       string, which the extension reads as "exclude nothing"; leaving
       the key unset instead would let a stale parent value decide.

    ``GITHUB_TOKEN``, ``GH_TOKEN``, ``ACTIONS_RUNTIME_TOKEN``, arbitrary
    user secrets, and any key not on the allowlist are absent from the
    returned env. ``os.environ`` is not mutated.

    LLM_API_KEY is forwarded; pi consumes it via the readonly-tools
    extension's ``apiKey: "LLM_API_KEY"`` registration (the literal key
    never appears in argv).
    """
    parent = os.environ
    out: dict[str, str] = {}

    for k in PI_ENV_ALWAYS_ALLOW:
        v = parent.get(k)
        if v is not None:
            out[k] = v

    out.update({k: v for k, v in parent.items() if k.startswith(PI_ENV_LC_PREFIX)})

    raw = parent.get("MOMUS_PI_ENV_PASSTHROUGH", "")
    for token in (t.strip() for t in raw.split(",") if t.strip()):
        if not _PASSTHROUGH_KEY_RE.match(token):
            log.info("pi_env_passthrough_skipped_invalid_key key=%s", token)
            continue
        if (
            token in PI_ENV_ALWAYS_ALLOW
            or token.startswith(PI_ENV_LC_PREFIX)
            or token in _RESERVED_PASSTHROUGH
        ):
            log.info("pi_env_passthrough_skipped_reserved key=%s", token)
            continue
        v = parent.get(token)
        if v is not None:
            out[token] = v

    out["MOMUS_WORK_DIR"] = str(work_dir.relative_to(repo_root))
    out["MOMUS_EXCLUDE_PATHS"] = "\n".join(exclude_paths or [])
    out.setdefault("LLM_BASE_URL", DEFAULT_LLM_BASE_URL)
    out.setdefault("LLM_MODEL", DEFAULT_LLM_MODEL)
    return out
