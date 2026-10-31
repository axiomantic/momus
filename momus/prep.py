"""Build phase inputs (diff, changed-files, conventions, pr-meta, rendered prompts)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .config import Config
from .diff_filter import DiffFilter
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

    # One filter, applied at both write sites. diff.patch and
    # changed-files.txt describe the same review scope; building the
    # predicate twice is how they would come to disagree.
    diff_filter = DiffFilter(
        patterns=tuple(config.scope.exclude_paths),
        exclude_binary=config.scope.exclude_binary_files,
    )
    dropped = _write_diff(repo_root, base_sha, head_sha, inputs_dir / "diff.patch", diff_filter)
    _write_changed_files(
        repo_root,
        base_sha,
        head_sha,
        inputs_dir / "changed-files.txt",
        diff_filter,
        dropped,
    )
    _write_conventions(repo_root, config, inputs_dir / "conventions.md")
    (inputs_dir / "pr-meta.json").write_text(json.dumps(pr_meta, indent=2))

    prompts_dir = inputs_dir / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    run_id = pr_meta.get("run_id", "A")
    for phase in ("phase1", "phase2", "phase3"):
        # Phase 1 needs the absolute work_dir so render_phase_prompt can
        # inline `inputs/prior-threads.json` between fence markers when
        # the prompt uses <<UNTRUSTED_PRIOR_THREADS_JSON>>. Other phases
        # do not reference that placeholder and pass None (the default).
        phase_work_dir = work_dir if phase == "phase1" else None
        rendered = render_phase_prompt(phase, config, run_id, work_dir_rel, work_dir=phase_work_dir)
        (prompts_dir / f"{phase}.md").write_text(rendered)

    return inputs_dir


def _write_diff(
    repo_root: Path,
    base: str,
    head: str,
    dest: Path,
    diff_filter: DiffFilter | None = None,
) -> frozenset[str]:
    """Write the review patch; return the paths the filter dropped.

    The returned set is what ``_write_changed_files`` needs to reach the
    same verdict on binary files, which it cannot derive from
    ``--name-only`` output on its own.
    """
    proc = subprocess.run(
        ["git", "diff", "--no-color", f"{base}...{head}"],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo_root,
    )
    if diff_filter is None:
        dest.write_text(proc.stdout)
        return frozenset()
    patch, dropped = diff_filter.filter_patch(proc.stdout)
    dest.write_text(patch)
    return dropped


def _write_changed_files(
    repo_root: Path,
    base: str,
    head: str,
    dest: Path,
    diff_filter: DiffFilter | None = None,
    dropped: frozenset[str] = frozenset(),
) -> None:
    proc = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo_root,
    )
    if diff_filter is None:
        dest.write_text(proc.stdout)
        return
    dest.write_text(diff_filter.filter_changed_files(proc.stdout, dropped))


def _write_conventions(repo_root: Path, config: Config, dest: Path) -> None:
    parts: list[str] = []
    seen: set[Path] = set()

    for rel in config.conventions.files:
        path = (repo_root / rel).resolve()
        if not _is_under(path, repo_root) or not path.exists() or path in seen:
            continue
        seen.add(path)
        parts.append(
            _format_convention_section(
                path.relative_to(repo_root), path.read_text(encoding="utf-8")
            )
        )

    for pattern in config.conventions.globs:
        for match in sorted(repo_root.glob(pattern)):
            path = match.resolve()
            if not _is_under(path, repo_root) or not path.is_file() or path in seen:
                continue
            seen.add(path)
            parts.append(
                _format_convention_section(
                    path.relative_to(repo_root), path.read_text(encoding="utf-8")
                )
            )

    dest.write_text("\n".join(parts) if parts else "")


def _format_convention_section(rel_path: Path, content: str) -> str:
    return f"# === {rel_path} ===\n\n{content}\n"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
