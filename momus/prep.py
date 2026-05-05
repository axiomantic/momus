"""Build phase inputs (diff, changed-files, conventions, pr-meta, rendered prompts)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .config import Config
from .render import render_phase_prompt


def prep_inputs(
    repo_root: Path,
    work_dir: Path,
    work_dir_rel: Path,
    pr_meta: dict[str, Any],
    config: Config,
) -> Path:
    """
    Materialize all inputs for the LLM phases under ``work_dir/inputs``.

    ``work_dir_rel`` is ``work_dir`` expressed relative to ``repo_root``
    (e.g. ``Path(".momus")``). It is substituted into the rendered phase
    prompts so the model references inputs/outputs as
    ``<work_dir_rel>/inputs/...`` from pi's CWD (= repo_root).

    Returns the inputs directory path.
    """
    inputs_dir = work_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)

    base_sha = pr_meta["base_sha"]
    head_sha = pr_meta["head_sha"]

    _write_diff(repo_root, base_sha, head_sha, inputs_dir / "diff.patch")
    _write_changed_files(repo_root, base_sha, head_sha, inputs_dir / "changed-files.txt")
    _write_conventions(repo_root, config, inputs_dir / "conventions.md")
    (inputs_dir / "pr-meta.json").write_text(json.dumps(pr_meta, indent=2))

    prompts_dir = inputs_dir / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    run_id = pr_meta.get("run_id", "A")
    for phase in ("phase1", "phase2", "phase3"):
        rendered = render_phase_prompt(phase, config, run_id, work_dir_rel)
        (prompts_dir / f"{phase}.md").write_text(rendered)

    return inputs_dir


def _write_diff(repo_root: Path, base: str, head: str, dest: Path) -> None:
    proc = subprocess.run(
        ["git", "diff", "--no-color", f"{base}..{head}"],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo_root,
    )
    dest.write_text(proc.stdout)


def _write_changed_files(repo_root: Path, base: str, head: str, dest: Path) -> None:
    proc = subprocess.run(
        ["git", "diff", "--name-only", f"{base}..{head}"],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo_root,
    )
    dest.write_text(proc.stdout)


def _write_conventions(repo_root: Path, config: Config, dest: Path) -> None:
    parts: list[str] = []
    seen: set[Path] = set()

    for rel in config.conventions.files:
        path = (repo_root / rel).resolve()
        if not _is_under(path, repo_root) or not path.exists() or path in seen:
            continue
        seen.add(path)
        parts.append(_format_convention_section(path.relative_to(repo_root), path.read_text(encoding="utf-8")))

    for pattern in config.conventions.globs:
        for match in sorted(repo_root.glob(pattern)):
            path = match.resolve()
            if not _is_under(path, repo_root) or not path.is_file() or path in seen:
                continue
            seen.add(path)
            parts.append(_format_convention_section(path.relative_to(repo_root), path.read_text(encoding="utf-8")))

    dest.write_text("\n".join(parts) if parts else "")


def _format_convention_section(rel_path: Path, content: str) -> str:
    return f"# === {rel_path} ===\n\n{content}\n"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
