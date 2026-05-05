"""Doc-consistency regressions for the injection-hardening migration.

These tests defend invariants in user-facing documentation that are
load-bearing for the security claims of the W4 hardening work. They
are intentionally narrow: full prose review lives in code review, not
in pytest.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_readme_no_longer_lists_gh_in_bash_ro_allowlist():
    """README must not advertise `gh` inside the `bash_ro` binary
    allowlist enumeration.

    Rationale (design §W4 W4-README-fix): `gh` was removed from the
    bash_ro allowlist as part of the contained-tools work. `gh` is
    invoked from Python pre-pi (fetch_priors.py); the LLM phases never
    need it. Leaving the README claim that bash_ro includes `gh` would
    misrepresent the runtime sandbox surface.

    The check looks at every parenthesized list that follows the
    phrase ``allowlisted binaries`` after a ``bash_ro`` mention.
    """
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    # Find every parenthesized binary list after "bash_ro" + "allowlisted binaries".
    pattern = re.compile(
        r"bash_ro[^\n]{0,200}?allowlisted binaries[^\(]*\(([^)]+)\)",
        flags=re.S,
    )
    matches = list(pattern.finditer(text))
    assert matches, (
        "README has no bash_ro allowlist enumeration to validate; "
        "test pattern likely needs an update"
    )
    for m in matches:
        listing = m.group(1)
        assert "`gh`" not in listing and " gh " not in listing, (
            f"bash_ro allowlist enumeration still includes gh:\n  {listing}"
        )


def test_readme_documents_pi_env_passthrough_escape_hatch():
    """README must document `MOMUS_PI_ENV_PASSTHROUGH` so users with
    custom env-var dependencies have an opt-in path post-W3 default-
    deny allowlist."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "MOMUS_PI_ENV_PASSTHROUGH" in text, (
        "README must mention MOMUS_PI_ENV_PASSTHROUGH (W3 escape hatch)"
    )


def test_changelog_has_v1_1_0_entry_with_hardening_bullets():
    """CHANGELOG must list the v1.1.0 release with all four hardening
    bullets called out in design §11."""
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "1.1.0" in text, "CHANGELOG missing v1.1.0 entry"
    # Spot-check the four hardening areas. Each must appear by name
    # somewhere in the v1.1.0 region (we do not pin the order).
    expected_topics = [
        "MOMUS_PI_ENV_PASSTHROUGH",
        "read_repo",  # contained tools
        "redact",  # publish redaction
        "adversarial",  # corpus harness
    ]
    for topic in expected_topics:
        assert topic in text, f"CHANGELOG v1.1.0 missing topic: {topic}"


def test_agents_md_has_adversarial_corpus_section():
    """AGENTS.md must point at the adversarial corpus harness so future
    contributors know where the security regression suite lives."""
    text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "Adversarial corpus" in text or "adversarial corpus" in text, (
        "AGENTS.md missing adversarial-corpus section"
    )
    assert "redteam-corpus.yml" in text, (
        "AGENTS.md must reference the weekly cron workflow"
    )
    assert "pytest -m adversarial" in text, (
        "AGENTS.md must document the local trigger"
    )
