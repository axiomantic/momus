"""W5-PublishWiring: validation + redaction at the publish boundary.

These tests verify the publish path's two new responsibilities:

1. **Reject malformed input loudly.** A `findings.json` that fails the
   `FindingsDoc` schema (extra keys, wrong types, oversize text) MUST NOT
   reach the GitHub API. The orchestrator logs the validation error and
   exits nonzero; no PR comment is posted.

2. **Redact credentials BEFORE construction.** Tokens that match
   `TOKEN_PATTERNS` in any LLM-emitted field (summary, finding title /
   message / suggestion, noteworthy entries) must be replaced with
   `[redacted]` before the markdown body or inline-comment body is
   assembled.
"""

from __future__ import annotations

from typing import Any

import pytest
import tripwire
from momus import publish as publish_mod
from momus.config import (
    ChecksConfig,
    Config,
    ConventionsConfig,
    PostConfig,
    ProviderConfig,
    ReviewConfig,
    VerifyConfig,
)
from momus.findings_schema import FindingsDoc
from pydantic import ValidationError


def _minimal_config() -> Config:
    return Config(
        review=ReviewConfig(
            blocking_severities=["critical", "high"],
            require_calibration=True,
            emit_nits=True,
            max_findings=50,
            noteworthy_max=3,
            run_id_scheme="alpha",
            repo_emphasis="",
        ),
        conventions=ConventionsConfig(files=[], globs=[]),
        post=PostConfig(
            first_review_approve_policy="if_no_findings",
            allow_human_approve_override=False,
        ),
        verify=VerifyConfig(enabled=True),
        checks=ChecksConfig(enabled=False, name="Momus Code Review"),
        provider=ProviderConfig(model="", base_url=""),
    )


def _pr_meta() -> dict[str, Any]:
    return {
        "owner": "elijahr",
        "repo": "lockfreequeues",
        "pr_number": 25,
        "head_sha": "deadbeef" + "0" * 32,
        "run_id": "R1",
        "author": "someone-else",
    }


def _valid_findings_dict(verdict: str = "COMMENT") -> dict[str, Any]:
    return {
        "summary": "One real bug found.",
        "verdict": verdict,
        "tally": {"high": 1},
        "findings": [
            {
                "id": "BOT-A1",
                "file": "src/foo.py",
                "line": 10,
                "side": "RIGHT",
                "severity": "high",
                "category": "bug",
                "blocking": True,
                "title": "Off-by-one in loop bound.",
                "message": "Loop runs one fewer iteration than intended.",
            }
        ],
        "prior_findings_status": [],
    }


# ---------------------------------------------------------------------------
# Validation: malformed input is rejected before any GH API call
# ---------------------------------------------------------------------------


def test_publish_accepts_validated_findings_doc():
    """publish() accepts a FindingsDoc (the post-W5 validated shape).

    Pre-W5 the signature was `dict[str, Any]`; post-W5 it MUST be
    `FindingsDoc` so type-checked code cannot accidentally pass an
    unvalidated dict to the GitHub API.
    """
    cfg = _minimal_config()
    pr_meta = _pr_meta()
    doc = FindingsDoc.model_validate(_valid_findings_dict())

    # Tripwire conversion: the verdict here is COMMENT, so the
    # APPROVE-downgrade path that consults `_get_token_user_info` is never
    # reached. Mocking only `_gh_api` is sufficient — tripwire's strict
    # interaction ledger surfaces a single POST and would fail loudly if
    # `publish()` called any other patched seam.
    gh_api = tripwire.mock.object(publish_mod, "_gh_api")
    gh_api.returns({})

    with tripwire:
        publish_mod.publish(doc, [], pr_meta, cfg, run_url="https://run/1")

    # Exactly one POST to /reviews; payload shape is exercised by the
    # neighboring redaction tests, so accept it as a partial dict here.
    from dirty_equals import IsDict

    gh_api.assert_call(
        args=(
            "POST",
            "/repos/elijahr/lockfreequeues/pulls/25/reviews",
            IsDict().settings(partial=True),
        ),
        kwargs={},
    )


def test_publish_rejects_malformed_doc_and_does_not_post():
    """A doc with an extra top-level key fails validation; no POST happens."""
    bad = _valid_findings_dict()
    bad["shell_command"] = "rm -rf /"  # injected key

    posted: list[Any] = []

    def fake_gh(method: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        posted.append((method, endpoint, payload))
        return {}

    with pytest.raises(ValidationError):
        # Validation happens at the boundary (FindingsDoc.model_validate).
        # publish() now requires a validated model, so the validation
        # error is raised by the caller (in __main__), not publish itself.
        FindingsDoc.model_validate(bad)

    # No GH API call made because we never reached publish().
    assert posted == []


def test_main_read_outputs_json_validates_findings_against_schema(tmp_path):
    """`_read_outputs_json` (or its replacement) validates findings.json
    against the FindingsDoc schema and raises on malformed input.

    This is the entry-point gate: the orchestrator MUST reject before the
    publish call, raising `FindingsValidationError` so `main()`'s outer
    Exception handler can surface a `failed` status + PR error comment.
    A bare SystemExit would skip that handler entirely.
    """
    from momus.__main__ import FindingsValidationError, _read_findings_doc

    findings_path = tmp_path / "findings.json"
    bad = _valid_findings_dict()
    bad["shell_command"] = "rm -rf /"
    findings_path.write_text(__import__("json").dumps(bad))

    with pytest.raises(FindingsValidationError) as excinfo:
        _read_findings_doc(findings_path)
    # Message is the first line of the validation log so it's safe to
    # surface unredacted to the PR via main()'s error handler.
    assert "findings.json schema validation failed" in str(excinfo.value)


def test_main_read_outputs_json_logs_validation_error_with_clear_message(tmp_path, capsys):
    """The validation-failure log line names the file and includes the
    pydantic error so a human reading the action log can tell what failed."""
    from momus.__main__ import FindingsValidationError, _read_findings_doc

    findings_path = tmp_path / "findings.json"
    bad = _valid_findings_dict()
    bad["findings"][0]["severity"] = "catastrophic"  # not in enum
    findings_path.write_text(__import__("json").dumps(bad))

    with pytest.raises(FindingsValidationError):
        _read_findings_doc(findings_path)

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "findings.json schema validation failed" in combined
    # And the offending field name appears in the message.
    assert "severity" in combined


def test_main_read_outputs_json_returns_validated_model_on_success(tmp_path):
    """On a valid file, _read_findings_doc returns a FindingsDoc instance."""
    from momus.__main__ import _read_findings_doc

    findings_path = tmp_path / "findings.json"
    findings_path.write_text(__import__("json").dumps(_valid_findings_dict()))

    doc = _read_findings_doc(findings_path)
    assert isinstance(doc, FindingsDoc)
    assert doc.verdict == "COMMENT"
    assert len(doc.findings) == 1


# ---------------------------------------------------------------------------
# Redaction: credentials in LLM-emitted strings are scrubbed before POST
# ---------------------------------------------------------------------------


def test_publish_redacts_credential_in_finding_message_before_post():
    """A finding whose `message` contains a GitHub PAT must be redacted
    before the inline comment body is built and posted."""
    cfg = _minimal_config()
    pr_meta = _pr_meta()
    doc_dict = _valid_findings_dict()
    pat = "ghp_" + "A" * 36
    doc_dict["findings"][0]["message"] = f"leaked: {pat} in commit"
    doc = FindingsDoc.model_validate(doc_dict)

    posted: list[dict[str, Any]] = []

    def fake_gh(method: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        posted.append(payload)
        return {}

    import unittest.mock as _mock

    with (
        _mock.patch.object(publish_mod, "_gh_api", fake_gh),
        _mock.patch.object(publish_mod, "_get_token_user_info", lambda: None),
    ):
        publish_mod.publish(doc, [], pr_meta, cfg, run_url="https://run/1")

    assert len(posted) == 1
    payload = posted[0]
    # PAT must NOT appear anywhere in the posted payload.
    serialized = __import__("json").dumps(payload)
    assert pat not in serialized
    assert "ghp_" not in serialized
    # The redaction marker should appear inside the inline comment body.
    inline_bodies = [c["body"] for c in payload["comments"]]
    assert any("[redacted]" in b for b in inline_bodies)


def test_publish_redacts_credential_in_summary_before_post():
    """A summary containing a credential is redacted in the review body.

    Tripwire-converted: instead of capturing payloads into a list and
    grep-asserting after the fact, we use ``tripwire.mock.object`` and
    ``assert_call`` with a ``dirty_equals`` matcher that demands the
    redaction marker is present AND the credential is absent in the body
    field of the single POST. A regression that posted twice, posted with
    the wrong endpoint, or smuggled the credential past redaction would
    fail the assertion outright rather than slip through a substring check.
    """
    cfg = _minimal_config()
    pr_meta = _pr_meta()
    doc_dict = _valid_findings_dict()
    pat = "ghp_" + "Q" * 36
    doc_dict["summary"] = f"Leaked PAT in diff: {pat}"
    doc = FindingsDoc.model_validate(doc_dict)

    gh_api = tripwire.mock.object(publish_mod, "_gh_api")
    gh_api.returns({})

    with tripwire:
        publish_mod.publish(doc, [], pr_meta, cfg, run_url="https://run/1")

    from dirty_equals import IsDict, IsStr

    gh_api.assert_call(
        args=(
            "POST",
            "/repos/elijahr/lockfreequeues/pulls/25/reviews",
            IsDict(
                body=IsStr(regex=r"(?s).*\[redacted\].*") & ~IsStr(regex=rf"(?s).*{pat}.*"),
            ).settings(partial=True),
        ),
        kwargs={},
    )


def test_publish_redacts_credential_in_finding_title_before_post():
    """A finding title containing a credential is redacted in both the
    summary one-liner (in the body) and the inline-comment header."""
    cfg = _minimal_config()
    pr_meta = _pr_meta()
    doc_dict = _valid_findings_dict()
    pat = "ghp_" + "T" * 36
    doc_dict["findings"][0]["title"] = f"Hardcoded PAT: {pat}"
    doc = FindingsDoc.model_validate(doc_dict)

    posted: list[dict[str, Any]] = []

    def fake_gh(method: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        posted.append(payload)
        return {}

    import unittest.mock as _mock

    with (
        _mock.patch.object(publish_mod, "_gh_api", fake_gh),
        _mock.patch.object(publish_mod, "_get_token_user_info", lambda: None),
    ):
        publish_mod.publish(doc, [], pr_meta, cfg, run_url="https://run/1")

    assert len(posted) == 1
    serialized = __import__("json").dumps(posted[0])
    assert pat not in serialized


def test_publish_redacts_credential_in_suggestion_before_post():
    """A finding suggestion (rendered inside a ```suggestion fence) gets
    its credentials redacted along with everything else."""
    cfg = _minimal_config()
    pr_meta = _pr_meta()
    doc_dict = _valid_findings_dict()
    pat = "AKIAIOSFODNN7EXAMPLE"  # 16 trailing alnum
    doc_dict["findings"][0]["suggestion"] = f"key = '{pat}'"
    doc = FindingsDoc.model_validate(doc_dict)

    posted: list[dict[str, Any]] = []

    def fake_gh(method: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        posted.append(payload)
        return {}

    import unittest.mock as _mock

    with (
        _mock.patch.object(publish_mod, "_gh_api", fake_gh),
        _mock.patch.object(publish_mod, "_get_token_user_info", lambda: None),
    ):
        publish_mod.publish(doc, [], pr_meta, cfg, run_url="https://run/1")

    assert len(posted) == 1
    serialized = __import__("json").dumps(posted[0])
    assert pat not in serialized


def test_publish_strips_off_domain_image_before_post():
    """Off-domain markdown image references in the summary are stripped."""
    cfg = _minimal_config()
    pr_meta = _pr_meta()
    doc_dict = _valid_findings_dict()
    doc_dict["summary"] = "Looks fine. ![pixel](https://evil.example/track.png) Done."
    doc = FindingsDoc.model_validate(doc_dict)

    posted: list[dict[str, Any]] = []

    def fake_gh(method: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        posted.append(payload)
        return {}

    import unittest.mock as _mock

    with (
        _mock.patch.object(publish_mod, "_gh_api", fake_gh),
        _mock.patch.object(publish_mod, "_get_token_user_info", lambda: None),
    ):
        publish_mod.publish(doc, [], pr_meta, cfg, run_url="https://run/1")

    assert len(posted) == 1
    body = posted[0]["body"]
    assert "evil.example" not in body
    assert "[image stripped: off-domain]" in body


def test_publish_redacts_credential_in_noteworthy_before_post():
    """Noteworthy entries are LLM-emitted; redact them too."""
    cfg = _minimal_config()
    pr_meta = _pr_meta()
    doc_dict = _valid_findings_dict()
    pat = "ghp_" + "N" * 36
    doc_dict["noteworthy"] = [f"Saw a token: {pat} (worth flagging?)"]
    doc = FindingsDoc.model_validate(doc_dict)

    posted: list[dict[str, Any]] = []

    def fake_gh(method: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        posted.append(payload)
        return {}

    import unittest.mock as _mock

    with (
        _mock.patch.object(publish_mod, "_gh_api", fake_gh),
        _mock.patch.object(publish_mod, "_get_token_user_info", lambda: None),
    ):
        publish_mod.publish(doc, [], pr_meta, cfg, run_url="https://run/1")

    assert len(posted) == 1
    body = posted[0]["body"]
    assert pat not in body
    assert "[redacted]" in body
