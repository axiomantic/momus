# Modifying prompts

The phase prompts under `momus/prompts/*.md` and the emphasis modules
under `momus/prompts/emphasis/*.md` are momus's source code in the
sense that matters most: at runtime, the LLM is the engine, and the
prompts are what it executes. This page is about changing them
without breaking review behavior.

## Why this is higher-stakes than code changes

Python tests assert on Python behavior. Prompt edits change LLM
behavior, and LLM behavior is harder to pin down. The render layer
guarantees the prompt was assembled correctly; the corpus guarantees
the assembled prompt produced contained behavior on a fixed set of
attacks. Neither catches "the bot now ignores nits when it should
have flagged them." That gap is the reason prompt changes need more
care than code changes.

Voice matters as much as content. The prompts speak to the model in
a register; changes that drift in tone (preachier, hedgier, longer)
shift behavior even when the literal content is unchanged.

## What the test suite catches

Two test layers cover prompts:

**Render tests** (`tests/test_render.py`). These assert the prompt
was assembled correctly: every `<<TOKEN>>` is substituted with a
plausible value, every config combination produces a valid render,
no orphan tokens leak through. The runtime token guard at
`momus/render.py:52-57` raises `ValueError` if any `<<TOKEN>>`
survives substitution; render tests exercise this fail-closed path.

**Adversarial corpus** (`tests/adversarial/`). The behavioral check.
Six attacker-goal fixtures plus a smoke fixture exercise the prompt
under hostile conditions. Run with `uv run pytest -m adversarial`.
See `adversarial-corpus.md`.

What the suite does NOT catch: subtle quality drift on benign PRs.
If your prompt edit makes the bot more lenient on a class of real
bugs, no test will fire. The mitigation is: small edits, run the
corpus, and read what changes in published findings on a real PR
before merging.

## Token discipline

Every `<<TOKEN>>` in a prompt must be substituted by
`momus/render.py`'s `_substitutions` (or by the prior-threads handler
for `<<UNTRUSTED_PRIOR_THREADS_JSON>>`). The runtime guard fails the
phase if any token survives.

If you add a new token to a prompt, you MUST also add the
substitution in `_substitutions` (`momus/render.py:96-201`) and a
render test that exercises it. If you delete a token from a prompt
without removing its substitution, that's harmless: the substitution
runs, finds no occurrences, and the dictionary entry is dead. Worth
cleaning up but not load-bearing.

The full token list is in
`../reference/prompt-tokens.md`. Cross-link your new token there if
you add one.

## Voice rules

The prompts speak in an imperative register. Stay there.

- **Terse.** No hedging, no padding. "Do X" rather than "You should
  consider doing X."
- **Imperative.** Direct commands to the model: "Read the file."
  "Quote the line." "Drop the finding." Avoid soft framings
  ("perhaps", "where appropriate", "if relevant").
- **Concrete.** Name files, names, numbers. "Cite line N" rather
  than "cite the relevant line."
- **No persona drift.** The prompts do not try to be cheerful or
  apologetic. They describe what counts as a finding and what does
  not.

Read `momus/prompts/phase2-review.md` end to end before editing it.
Match the existing register exactly. The existing prompt is the
spec for the voice.

## Emphasis modules vs phase-prompt edits

There are two ways to change review behavior:

**Sharpen a phase prompt.** Edit `phase2-review.md` or
`phase3-verify.md` directly. This applies to every review momus
runs. Use this for review discipline that is universal: how to
ground findings, what counts as a verifying observation, how to
treat prior `DECLINED` items.

**Add or edit an emphasis module.** Modules live in
`momus/prompts/emphasis/` and are opt-in via
`review.emphasis_modules` in a repo's `.momus.yaml`. Each module is
a SHIPPABLE unit of focus (currently four:
[`security`](../reference/emphasis-modules.md#security),
[`dead_code`](../reference/emphasis-modules.md#dead-code),
[`quality_checklist`](../reference/emphasis-modules.md#quality-checklist),
[`test_quality`](../reference/emphasis-modules.md#test-quality)).
Use this for focus that some repos want and others don't:
OWASP-style security review, language-specific test smells, dead
code hunting.

The decision rule is simple: if every momus user benefits, sharpen
the phase prompt. If the focus is opinionated and only some repos
want it, ship a module.

When you add a module, register it in `review.emphasis_modules`'s
docstring (`momus/config-defaults.yaml`) and add the reference page
section under `../reference/emphasis-modules.md`.

## The phase-2 + phase-3 coordination pattern

A new requirement in phase 2 is enforceable only if phase 3 has a
matching DROP rule. Phase 2 says "do X"; phase 3 audits whether the
finding actually shows X and drops it if not. Either side alone is
half a feature.

The verifying-observation rule on this branch is the worked example.
Phase 2 (`momus/prompts/phase2-review.md:117-123`) tells the model:

> Ground every finding. The `message` field MUST contain BOTH (a)
> the hypothesis (what's wrong) AND (b) the verifying observation
> that grounds it. The verifying observation MUST be one of two
> forms: (1) a quoted line from the cited file (preferred), or
> (2) a grep result with the matched line. ... Findings that assert
> without grounding will be DROPPED in phase 3.

The matching phase 3 DROP rule
(`momus/prompts/phase3-verify.md:89-100`) is:

> **Verifying-observation grounding.** ... DROP any finding whose
> `message` does not contain a verifying observation: a quoted line
> from the cited file, OR a grep result with the matched line.

Without phase 3's drop, phase 2's instruction is a polite suggestion
the model can ignore. With both, it is enforced: ungrounded findings
do not survive verify.

When you add a "do X" requirement to phase 2, search phase 3 for the
matching audit rule. If there isn't one, write it. If you cannot
state a phase-3 audit in concrete terms ("DROP if message lacks
substring matching pattern P"), the requirement is too soft to be
enforced; rewrite it until you can.

## Workflow checklist

Before opening a PR that edits a prompt:

1. `uv run pytest tests/test_render.py` passes.
2. The corpus smoke fixture passes:
   `uv run pytest -m adversarial -k smoke`.
3. If the change touches injection containment, run the full
   corpus: `uv run pytest -m adversarial`. (Costs LLM budget; do
   this once before PR, not on every iteration.)
4. CHANGELOG entry under `### Changed` for any user-visible behavior
   shift.
5. If you added a token, module, or reference: cross-link from the
   relevant `docs/reference/` page so the anchor map stays
   complete.
