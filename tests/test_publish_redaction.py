"""Redaction tests for momus.publish.redact_for_publish (W5-Redaction).

Per design §W5: scrub credential-shaped strings (GitHub PATs, OpenAI sk-,
AWS AKIA) and strip off-domain markdown image references from
publisher-bound text. Off-domain image stripping defends against the
CamoLeak class of exfiltration where the model embeds an image
referencing an attacker-controlled domain; GitHub renders the request
URL and leaks any path-encoded data.

False-positive guards are non-negotiable: redacting `sk_buffer` (a
common identifier in code) or `AKIATooShort` would corrupt legitimate
review prose.
"""

from __future__ import annotations

from momus.checks import _build_summary, _finding_one_liner as _checks_one_liner
from momus.publish import (
    _finding_inline_body,
    _finding_one_liner,
    redact_for_publish,
)


def test_redaction_redacts_full_ghp_pat():
    text = "leaked PAT: ghp_" + "A" * 36 + " in commit message"
    out, n = redact_for_publish(text)
    assert "ghp_" not in out
    assert "[redacted]" in out
    assert n >= 1


def test_redaction_redacts_full_gho_oauth_token():
    text = "oauth: gho_" + "B" * 36
    out, n = redact_for_publish(text)
    assert "gho_" not in out
    assert "[redacted]" in out
    assert n == 1


def test_redaction_redacts_full_ghu_user_token():
    text = "user: ghu_" + "C" * 36
    out, _ = redact_for_publish(text)
    assert "ghu_" not in out


def test_redaction_redacts_full_ghs_server_token():
    text = "server: ghs_" + "D" * 36
    out, _ = redact_for_publish(text)
    assert "ghs_" not in out


def test_redaction_redacts_full_ghr_refresh_token():
    text = "refresh: ghr_" + "E" * 36
    out, _ = redact_for_publish(text)
    assert "ghr_" not in out


def test_redaction_does_not_redact_partial_ghp_token():
    """Length matters: ghp_ followed by fewer than 36 chars must not match.

    Without this guard, any incidental `ghp_` prefix in code (a variable
    name, a short identifier) would get redacted.
    """
    text = "ghp_short and also ghp_only20chars01234567"
    out, n = redact_for_publish(text)
    assert out == text
    assert n == 0


def test_redaction_redacts_openai_sk_with_48_chars():
    text = "key: sk-" + "a" * 48 + " end"
    out, n = redact_for_publish(text)
    assert "sk-" not in out or "[redacted]" in out
    assert "a" * 48 not in out
    assert n >= 1


def test_redaction_redacts_openai_sk_with_more_than_48_chars():
    """The sk- pattern is {48,} so longer keys also redact."""
    text = "longer: sk-" + "z" * 60
    out, n = redact_for_publish(text)
    assert "z" * 60 not in out
    assert n >= 1


def test_redaction_does_not_redact_sk_buffer_identifier():
    """sk_buffer must NOT be redacted — `sk_` is a common code prefix.

    The regex requires the literal `sk-` (hyphen), not `sk_` (underscore),
    so this is the design's false-positive guard.
    """
    text = "the sk_buffer is misnamed; rename to ring_buffer."
    out, n = redact_for_publish(text)
    assert out == text
    assert n == 0


def test_redaction_redacts_aws_akia_with_exact_16_trailing_chars():
    text = "creds: AKIAIOSFODNN7EXAMPLE in env"
    out, n = redact_for_publish(text)
    assert "AKIA" not in out or "[redacted]" in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert n >= 1


def test_redaction_does_not_redact_AKIATooShort():
    """AKIA followed by fewer than 16 alnum chars must not match."""
    text = "AKIA12345"
    out, n = redact_for_publish(text)
    assert out == text
    assert n == 0


def test_redaction_does_not_redact_AKIA_with_lowercase_trailing():
    """The pattern requires [0-9A-Z]{16}; lowercase chars in trailing 16 fail."""
    text = "AKIAabcdefghijklmnop"  # 16 trailing but lowercase
    out, n = redact_for_publish(text)
    assert out == text
    assert n == 0


def test_redaction_strips_off_domain_image():
    """An off-domain ![](evil.example) image is stripped to an inline marker."""
    text = "Looks fine. ![pixel](https://evil.example/track.png) Done."
    out, _ = redact_for_publish(text)
    assert "evil.example" not in out
    assert "[image stripped: off-domain]" in out


def test_redaction_preserves_github_user_images():
    """user-images.githubusercontent.com is in the allowlist; preserved."""
    text = "Diagram: ![arch](https://user-images.githubusercontent.com/1/abc.png)"
    out, _ = redact_for_publish(text)
    assert "user-images.githubusercontent.com" in out
    assert "[image stripped" not in out


def test_redaction_preserves_github_com_images():
    """github.com (e.g., raw screenshots from issues) is in the allowlist."""
    text = "![logo](https://github.com/octocat/Hello-World/raw/main/logo.png)"
    out, _ = redact_for_publish(text)
    assert "github.com/octocat" in out
    assert "[image stripped" not in out


def test_redaction_handles_multiple_tokens_in_one_string():
    """Two distinct tokens in the same string are both redacted, count = 2."""
    text = "two: ghp_" + "A" * 36 + " and AKIAIOSFODNN7EXAMPLE here"
    out, n = redact_for_publish(text)
    assert "ghp_" not in out
    assert "AKIAIOSFODNN7" not in out
    assert n == 2


def test_redaction_returns_original_when_clean():
    """Clean text passes through unchanged with count 0."""
    text = "All clear, no findings."
    out, n = redact_for_publish(text)
    assert out == text
    assert n == 0


# ----------------------------------------------------------------------
# Field-level redaction in rendered bodies.
#
# `id`, `category`, and the top-level `summary` are LLM-emitted strings
# whose schema constraints permit credential-shaped substrings:
#
#   - id:       pattern `[A-Za-z0-9_-]+`, max 64 -- fits `ghp_<36>` exactly.
#   - category: free-form str, max 64.
#   - summary:  free-form str, max 2000.
#
# Every renderer that reaches GitHub must run them through
# `redact_for_publish` at construction time. These tests pin that
# invariant in publish.py (review body + inline comment) and
# checks.py (Check Run summary).
# ----------------------------------------------------------------------


_GHP_PAT = "ghp_" + "A" * 36


def _finding_with_credentials_in_metadata() -> dict:
    return {
        "id": _GHP_PAT,
        "file": "src/foo.py",
        "line": 12,
        "side": "RIGHT",
        "severity": "high",
        "category": f"hint: {_GHP_PAT}",
        "blocking": False,
        "title": "clean title",
        "message": "clean message",
    }


def test_finding_inline_body_redacts_id_field():
    body = _finding_inline_body(
        _finding_with_credentials_in_metadata(),
        run_url="https://example.test/r/1",
        run_id="X",
    )
    assert "ghp_" not in body
    assert "[redacted]" in body


def test_finding_inline_body_redacts_category_field():
    f = _finding_with_credentials_in_metadata()
    f["id"] = "BOT-1"
    body = _finding_inline_body(
        f, run_url="https://example.test/r/1", run_id="X"
    )
    assert "ghp_" not in body
    assert "[redacted]" in body


def test_finding_one_liner_redacts_id_field():
    line = _finding_one_liner(_finding_with_credentials_in_metadata())
    assert "ghp_" not in line
    assert "[redacted]" in line


def test_checks_finding_one_liner_redacts_id_field():
    line = _checks_one_liner(_finding_with_credentials_in_metadata())
    assert "ghp_" not in line
    assert "[redacted]" in line


def test_checks_build_summary_redacts_top_level_summary():
    findings_doc = {
        "summary": f"All looks good but {_GHP_PAT} sneaked in",
        "tally": {},
    }
    out = _build_summary(findings_doc, blocking_findings=[])
    assert "ghp_" not in out
    assert "[redacted]" in out


def test_checks_build_summary_redacts_blocking_finding_id():
    findings_doc = {"summary": "clean", "tally": {"high": 1}}
    blocking = [_finding_with_credentials_in_metadata()]
    out = _build_summary(findings_doc, blocking_findings=blocking)
    assert "ghp_" not in out
    assert "[redacted]" in out
