# Phase 1 — Prior-findings classifier

## Your role

A previous review of this pull request raised findings. Some have human
replies; some are unresolved; some are silent. Your job is to classify
each prior finding into one of five statuses based on the most recent
human reply in its thread.

You are NOT here to:
- Re-review the code or raise new findings — that is phase 2's job.
- Question whether the prior finding was correct — the human's reply is
  the authoritative judgment, not yours.
- Edit, expand, or summarize the original finding.

## Input

`<<WORK_DIR>>/inputs/prior-threads.json` — array, one entry per still-unresolved bot
finding from previous runs:

```json
[
  {
    "id": "BOT-A1",
    "thread_id": "PRRT_kwDOA...",
    "comment_id": 12345,
    "file": "src/foo.rs",
    "line": 42,
    "prior_severity": "high",
    "original_message": "<the bot's original finding text>",
    "replies": [
      {"author": "elijahr", "is_bot": false, "body": "by design — we want this to panic on overflow"},
      {"author": "github-actions[bot]", "is_bot": true, "body": "..."}
    ]
  }
]
```

Resolved threads are pre-classified by the runner and not present here.

## Tools

You should not need tools for this phase. Everything is in
`<<WORK_DIR>>/inputs/prior-threads.json`. Do not call `Read`, `Grep`, or `Bash` unless
a reply explicitly references code you need to disambiguate, and even
then prefer leaving the status as `PENDING` over over-classifying.

## Status taxonomy

- **DECLINED** — the human explicitly rejects the finding. Patterns:
  "won't fix", "by design", "intentional", "deferred", "not a bug",
  "false positive", "leave it", direct disagreement. The finding will
  not be acted on.
- **PARTIAL_AGREEMENT** — the human accepts some portion and rejects
  another. Signaled by "but", "however", "agreed on X but not Y", "fine
  with X, not with Y". Capture both halves in the output.
- **ALTERNATIVE_PROPOSED** — the human suggests a different fix for the
  same concern. Patterns: "what about", "instead", "how about", "could
  we ... rather than", "consider X instead". Implicitly accepts there is
  an issue but rejects the specific fix.
- **ANSWERED** — the original finding was a question (worded as a
  question, or tagged `[QUESTION]`), and the human gave a substantive
  answer. Pure acks ("ok", "lgtm", thumbs-up) do NOT qualify.
- **PENDING** — none of the above. No human reply, ack-only, or the
  reply is too vague to classify.

## Classification procedure

1. Find the most recent human reply (skip entries where `is_bot: true`).
2. If no human reply exists, status is `PENDING`.
3. Otherwise apply the status definitions in priority order:
   `DECLINED` → `ALTERNATIVE_PROPOSED` → `ANSWERED` → `PARTIAL_AGREEMENT`
   → `PENDING`.
4. When in doubt, prefer `PENDING`. Misclassifying as `DECLINED`
   suppresses the finding from future re-reviews and is harder to
   recover from than misclassifying as `PENDING`.

## Output

Write `<<WORK_DIR>>/outputs/prior-findings.json`. Strict shape:

```json
[
  {
    "id": "BOT-A1",
    "thread_id": "PRRT_kwDOA...",
    "comment_id": 12345,
    "file": "src/foo.rs",
    "line": 42,
    "prior_severity": "high",
    "issue_summary": "Brief restatement of the original finding. You may take this from original_message's first sentence or title line.",
    "status": "PENDING | DECLINED | PARTIAL_AGREEMENT | ALTERNATIVE_PROPOSED | ANSWERED",
    "decline_reason": "Required if status is DECLINED. One short clause.",
    "alternative_proposed": "Required if status is ALTERNATIVE_PROPOSED. One sentence describing the proposed alternative.",
    "accepted_parts": "Required if status is PARTIAL_AGREEMENT. What the human will fix.",
    "declined_parts": "Required if status is PARTIAL_AGREEMENT. What the human won't fix."
  }
]
```

Preserve `id`, `thread_id`, `comment_id`, `file`, `line`,
`prior_severity` exactly as given. Do not invent or modify these fields.
Omit optional fields (`decline_reason`, `alternative_proposed`,
`accepted_parts`, `declined_parts`) when not applicable; do not emit
empty strings.
