"""Invoke the pi CLI for one phase. Parses pi's --mode json event stream."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

EXTENSION_PATH = Path(__file__).resolve().parent.parent / "extensions" / "readonly-tools.ts"

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
    proc = subprocess.run(
        cmd,
        cwd=work_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    events = _parse_event_stream(proc.stdout)
    if proc.returncode != 0:
        last = events[-1] if events else {}
        raise PiInvocationError(
            f"phase {phase} failed with exit {proc.returncode}.\n"
            f"last event: {last}\n"
            f"stderr: {proc.stderr.strip()}"
        )
    return events


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


def _parse_event_stream(stdout: str) -> list[dict]:
    events: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            # pi may emit non-JSON lines on errors. Capture verbatim.
            events.append({"type": "_unparsed", "line": line})
    return events
