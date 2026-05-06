# Momus

Thorough AI code review as a GitHub Action. Provider-agnostic.

> Momus (/ˈmoʊməs/): The ancient Greek personification of relentless
> scrutiny. He was ultimately banished from Mount Olympus for his pedantic
> criticism of trivial defects in the gods' creations rather than assessing
> their functional intent.

The name is a self-aware caveat. The bot's failure mode is being Momus.
Momus the project is built to fight that failure mode at every layer.

## Why this exists

You have probably been burned by an AI review bot. A wall of low-confidence
nits, fabricated bugs that point at the wrong line, "consider extracting
this into a function" advice on every diff. Every comment costs the
reviewer's attention; the bot earns none of it back.

Momus is shaped by four claims, and the claims are checkable.

**The verify pass cannot fabricate.** Phase 3 audits phase 2's findings
against the source. It can drop a finding, demote its severity, strip an
unverified suggestion, or consolidate duplicates. It cannot promote a
finding's severity, and it cannot add a finding phase 2 did not produce.
The asymmetry is enforced by the prompt and bounded by the schema. See
[Review philosophy / Verify cannot promote](explanation/review-philosophy.md#verify-cannot-promote).

**The verify pass drops findings without grounding.** Every finding must
pair a hypothesis with quoted evidence from the actual code. A finding
that claims a problem without showing the line is not a verified
observation; it is vibes. Phase 3 drops vibes. This is the most direct
lever momus has against LLM hallucination, because hallucinations almost
always lack grounding. See
[Review philosophy / Verifying observations](explanation/review-philosophy.md#verifying-observations).

**The publisher is pure Python.** Phase 4 runs no LLM. It validates
findings against a Pydantic schema with `extra='forbid'` and length caps,
runs publish-time redaction over every LLM-emitted string (GitHub tokens,
OpenAI-shaped keys, AWS keys, off-domain images), and renders a single
GitHub Review object. If the LLM phases hallucinated wildly, the
deterministic layer is the floor. See
[The four-phase pipeline / Phase 4](explanation/four-phase-pipeline.md#phase-4-post).

**Blocking severities are repo-tunable, not bot-decided.** The severity
scale is fixed: `critical`, `high`, `medium`, `low`, `nit`. Which of those
block merge is a per-repo config knob (`review.blocking_severities`). A
repo with a strong CI suite can tighten to `[critical, high]`; another
repo can widen. Severity is a property of code in context. See
[Review philosophy / Severity and blocking](explanation/review-philosophy.md#severity-and-blocking).

The threat model that motivates the containment is documented and tested
against an [adversarial corpus](explanation/adversarial-corpus.md). Six
named attacker goals (G1, G2a, G2b, G2c, G3, G4) plus a smoke fixture run
weekly. Failures are real signal: either the bot broke or the corpus
broke, and someone needs to look.

## Start here

New to momus? Read the [Tutorial](tutorial/first-review.md). It walks you
from an empty repo to a real review on a real PR in about fifteen minutes.

## Where things live

The docs follow the [Diátaxis](https://diataxis.fr/) split.

- **[Tutorial](tutorial/first-review.md)** — start-to-finish walkthrough
  to your first real review. Read this first if you have not used momus.
- **[How-to](how-to/configure-blocking-severities.md)** — recipes for
  specific configuration tasks: tuning blocking severities, enabling the
  Check Run, overriding the provider per repo, setting up the GitHub App.
- **[Reference](reference/config-schema.md)** — every config key, every
  action input, every prompt token, every emphasis module. Look here when
  you know the name of a thing and need its exact semantics.
- **[Explanation](explanation/four-phase-pipeline.md)** — how momus
  works and why. The four-phase pipeline, the review philosophy, the
  threat model, the adversarial corpus. Read these when the docs above
  feel underspecified and you want to know whether to trust the design.

Status: under construction. Pilot on `elijahr/lockfreequeues`. Source on
[GitHub](https://github.com/axiomantic/momus).
