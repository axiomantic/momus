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
        assert "`gh`" not in listing, (
            f"bash_ro allowlist enumeration still includes gh:\n  {listing}"
        )
        assert " gh " not in listing, (
            f"bash_ro allowlist enumeration still includes gh:\n  {listing}"
        )


def test_readme_documents_pi_env_passthrough_escape_hatch():
    """README must document `MOMUS_PI_ENV_PASSTHROUGH` so users with
    custom env-var dependencies have an opt-in path post-W3 default-
    deny allowlist.

    The assertion is structural: there must be a paragraph (an
    Environment-scoping section) that introduces the variable AND
    explains how to set it. A bare mention in a code fence is not
    enough; users post-W3 will hit a no-vars-forwarded behavior change
    and need surrounding prose to know how to recover.
    """
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    # Environment scoping section must exist and contain the variable.
    section_pattern = re.compile(
        r"^##\s*Environment scoping\b.*?(?=^##\s)",
        flags=re.S | re.M,
    )
    section_match = section_pattern.search(text + "\n## END\n")
    assert section_match is not None, (
        "README must have an `## Environment scoping` section that "
        "documents the W3 default-deny allowlist."
    )
    section = section_match.group(0)
    assert "MOMUS_PI_ENV_PASSTHROUGH" in section, (
        "Environment scoping section must name MOMUS_PI_ENV_PASSTHROUGH"
    )
    # Must explain the syntax (comma-separated names) so users can act on it.
    assert "NAME1,NAME2" in section or "comma-separated" in section.lower(), (
        "Environment scoping section must explain the value format (comma-separated list of names)"
    )
    # Must call out that it is an opt-in escape hatch, not a flag to flip casually.
    assert "escape hatch" in section.lower() or "opt-in" in section.lower(), (
        "Environment scoping section must frame MOMUS_PI_ENV_PASSTHROUGH "
        "as an explicit opt-in escape hatch."
    )


def test_changelog_has_v1_1_0_entry_with_hardening_bullets():
    """CHANGELOG must list the v1.1.0 release with all four hardening
    bullets called out in design §11.

    Each topic must appear inside the v1.1.0 section specifically (not
    just somewhere in the file). Anchored search prevents a regression
    where the topics survive in old release notes while the v1.1.0
    section becomes a stub.
    """
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    # Carve the v1.1.0 section: from `## [1.1.0]` header to next `## [`.
    section_pattern = re.compile(
        r"^##\s*\[1\.1\.0\][^\n]*\n(.*?)(?=^##\s*\[)",
        flags=re.S | re.M,
    )
    match = section_pattern.search(text + "\n## [END]\n")
    assert match is not None, (
        "CHANGELOG must have a `## [1.1.0]` section header followed by a later `## [` section."
    )
    section = match.group(1)
    expected_topics = {
        "MOMUS_PI_ENV_PASSTHROUGH": "W3 default-deny env allowlist escape hatch",
        "read_repo": "W2 contained tools (read_repo, grep_repo, etc.)",
        "redact": "W5 credential redaction at the publish boundary",
        "adversarial": "W1 adversarial-corpus harness",
    }
    for topic, area in expected_topics.items():
        assert topic in section, (
            f"v1.1.0 section is missing reference to {area} (string '{topic}' not found)."
        )
    # Section must be substantive, not a one-line stub.
    assert section.count("\n") >= 10, (
        f"v1.1.0 section is suspiciously short "
        f"({section.count(chr(10))} lines); a real release entry covers "
        "Added/Changed/Fixed/Security buckets."
    )


def test_agents_md_has_adversarial_corpus_section():
    """AGENTS.md must point at the adversarial corpus harness so future
    contributors know where the security regression suite lives.

    The section header, the cron workflow path, and the local pytest
    invocation must all appear within the same anchored section.
    """
    text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    section_pattern = re.compile(
        r"^##\s*[Aa]dversarial corpus\b.*?(?=^##\s)",
        flags=re.S | re.M,
    )
    match = section_pattern.search(text + "\n## END\n")
    assert match is not None, "AGENTS.md must have an `## Adversarial corpus` section header."
    section = match.group(0)
    required_in_section = [
        "redteam-corpus.yml",
        "pytest -m adversarial",
        "MOMUS_REDTEAM_MOCK_PI",
    ]
    for needle in required_in_section:
        assert needle in section, f"Adversarial corpus section is missing `{needle}`."
