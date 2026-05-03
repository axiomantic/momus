"""Invoke the pi CLI for one phase. Parses pi's --mode json event stream."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

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
    return events


def _parse_one_line(line: str) -> dict:
    if not line.strip():
        return {"type": "_blank"}
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"type": "_unparsed", "line": line}


def _log_event(phase: str, event: dict, raw: str) -> None:
    """Print a one-line human summary for each pi event to stderr."""
    et = event.get("type", "?")
    if et == "_blank":
        return
    if et == "_unparsed":
        print(f"[momus.pi {phase}] (raw) {raw[:300]}", file=sys.stderr, flush=True)
        return
    # Best-effort summary: pull common fields if present, else show keys.
    summary_bits: list[str] = [f"type={et}"]
    for k in ("name", "tool", "phase", "model", "role", "status", "error"):
        if k in event and event[k]:
            v = str(event[k])
            if len(v) > 80:
                v = v[:77] + "..."
            summary_bits.append(f"{k}={v}")
    # If there's a text payload, show a snippet.
    text = event.get("text") or event.get("delta") or event.get("content")
    if isinstance(text, str) and text.strip():
        snippet = text.strip().replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:197] + "..."
        summary_bits.append(f'text="{snippet}"')
    print(f"[momus.pi {phase}] " + " ".join(summary_bits), file=sys.stderr, flush=True)


def invoke_pi_phase_with_retry(phase: str, work_dir: Path) -> list[dict]:
    """
    Defense-in-depth wrapper around ``invoke_pi_phase``. If the phase exits
    0 but the expected output file is missing (model ended its turn without
    calling ``write_output``), retry once with a hard reminder appended to
    the prompt. Phases not in ``PHASE_EXPECTED_OUTPUTS`` skip the guard.
    """
    events = invoke_pi_phase(phase, work_dir)
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
    events = invoke_pi_phase(phase, work_dir, extra_prompt_suffix=suffix)
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


