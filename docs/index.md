# Momus

Thorough AI code review as a GitHub Action.

Provider-agnostic: works with any OpenAI-compatible LLM (OpenRouter,
Anthropic, OpenAI, Bedrock, ...). Highly customizable.

> Momus (/ˈmoʊməs/): The ancient Greek personification of relentless
> scrutiny. He was ultimately banished from Mount Olympus for his
> pedantic criticism of trivial defects in the gods' creations rather
> than assessing their functional intent.

Status: under construction. Pilot on `elijahr/lockfreequeues`.

## Phases

1. **Plan** -- fetch prior bot reviews, classify each prior finding into
   `PENDING / DECLINED / PARTIAL_AGREEMENT / ALTERNATIVE_PROPOSED /
   ANSWERED`, produce a checklist for phase 2. Skipped on first review.
2. **Review** -- read the diff, investigate context with `Read` / `Grep`
   / sandboxed read-only `Bash`, produce structured findings with
   severity, category, and inline-comment-ready suggestions.
3. **Verify** -- audit phase 2's findings: drop false positives, demote
   over-severe findings, strip invalid suggestions, consolidate
   duplicates. Cannot promote or add findings.
4. **Post** -- deterministic Python publisher. Renders findings into a
   single GitHub Review object (APPROVE / REQUEST_CHANGES / COMMENT)
   with all inline comments attached.

Phases 1, 2, 3 are LLM calls. Phase 4 is pure code.

## Where to next

- [Usage](usage.md) -- triggers, configuration, environment.
- [Source on GitHub](https://github.com/axiomantic/momus) -- README,
  SETUP guide, and the action code.
