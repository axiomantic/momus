"""Behavioral CI check: pi v0.72.1 still structurally rejects unallowlisted tools.

These tests are pi-version-anchored. The unit-mark tests verify the pin file
contents directly (cheap, runs in normal CI). The integration-mark test
spawns pi to confirm that --tools really is an allowlist (skipped without
LLM_API_KEY because the pi run requires a live model).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from tripwire import M

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.mark.unit
def test_pi_version_pinned_to_0_72_1():
    pkg = REPO_ROOT / "package.json"
    data = json.loads(pkg.read_text())
    assert data["dependencies"]["@mariozechner/pi-coding-agent"] == "0.72.1"


@pytest.mark.unit
def test_package_lock_resolves_pi_to_exact_version():
    """D1 acceptance: package-lock.json must resolve pi at exactly 0.72.1."""
    lock = REPO_ROOT / "package-lock.json"
    data = json.loads(lock.read_text())
    pkgs = data.get("packages", {})
    pi_entry = pkgs.get("node_modules/@mariozechner/pi-coding-agent")
    assert pi_entry is not None, "pi not in package-lock.json"
    assert pi_entry.get("version") == "0.72.1"


@pytest.mark.integration
@pytest.mark.allow(M(protocol="subprocess", binary="pi"))
def test_pi_rejects_disallowed_tool(tmp_path: Path):
    """Confirm pi treats --tools as an allowlist by attempting an `edit` call.

    Skipped when LLM_API_KEY is unset because pi needs a live provider.
    """
    for var in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        if not os.environ.get(var):
            pytest.skip(f"{var} not set")
    ext = REPO_ROOT / "momus" / "extensions" / "readonly-tools.ts"
    result = subprocess.run(
        [
            "pi",
            "-p",
            "Use the `edit` tool to modify foo.txt. If `edit` is unavailable, "
            "say 'edit unavailable' and call write_output to outputs/done.json with {}.",
            "--provider",
            "byo",
            "-e",
            str(ext),
            "--tools",
            "read_repo,write_output",
            "--mode",
            "json",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    events = [
        json.loads(ln)
        for ln in result.stdout.splitlines()
        if ln.strip().startswith("{")
    ]
    tool_names = {e.get("tool") for e in events if e.get("tool")}
    assert "edit" not in tool_names
