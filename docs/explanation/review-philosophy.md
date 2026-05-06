# Review philosophy

Momus has opinions about what a code review is for. They show up in the prompts, in the schema, in which knobs exist and which do not. This page is the prose version.

## Signal over noise

A review with zero findings is a legitimate output. It means the diff looked fine.

This sounds obvious. It is not how most LLM review bots behave by default. The dominant failure mode of LLM review is a wall of low-confidence nits, hedge-everything suggestions, and "consider extracting this into a function" advice that costs the reviewer's attention without earning it. Momus pushes back on that in several places at once.

**Nits are off by default in spirit, on by default in config.** `review.emit_nits` defaults to `true` so that a fresh install does not silently change the visible severity scale, but `review.blocking_severities` defaults to `[critical, high, medium]`, so nits never block merge. A repo that wants nits gone entirely can set `review.emit_nits: false` and the prompt will instruct the model not to emit them at all. See `../reference/config-schema.md#review-emit-nits`.

**There is a hard cap on findings.** {#max-findings} `review.max_findings` defaults to 50 and is enforced at the publisher, not just suggested to the model. The prompt instructs the model to consolidate redundant items rather than truncate, but the cap exists because "the model decided to emit 200 findings" should never reach a real PR. See `../reference/config-schema.md#review-max-findings`.

**The model is asked to calibrate.** When `review.require_calibration` is true (the default), every blocking finding must include a one-line `calibration` justification answering "would a human reviewer genuinely block this PR over this?" That visible discipline is in the JSON output; reviewers can see it; phase 3 can read it. The discipline is the point. See [Calibration](#calibration) below.

The cumulative effect: when momus does post a finding, the reader can trust that several layers thought it was worth posting.

## Verifying observations {#verifying-observations}

This is the rule the current branch leans on hardest, and it is the one that most distinguishes momus's review behavior from a generic LLM-as-reviewer setup.

**Every finding must include a hypothesis paired with grounding.** A hypothesis is the model's claim about what the bug is. Grounding is the evidence: a quoted line from the file, a `grep_repo` result showing where else the symbol is used, a citation to a test that exercises the path. Findings that assert a problem without showing the line and explaining why are not verified observations; they are vibes.

Phase 3's job, in part, is to drop findings that fail this test. A finding that says "this function is probably racy" without naming the specific shared state and the specific concurrent caller is dropped. A finding that quotes a line and explains why that line cannot be reached from the entry points stays.

This is the most direct lever momus has against LLM hallucination. Hallucinations almost always lack grounding, because the grounding does not exist to be quoted. Demanding it as an entry condition removes a large class of plausible-sounding but wrong findings.

For the prompt-token plumbing that wires this into phase 2, see `../reference/prompt-tokens.md`.

## Severity and blocking {#severity-and-blocking}

The severity scale is `critical`, `high`, `medium`, `low`, `nit`. The scale is fixed; what each repo can configure is **which severities block merge**.

`review.blocking_severities` lists the severities that produce a `REQUEST_CHANGES` verdict. Anything not on the list is advisory: still posted, still visible, does not block. Default is `[critical, high, medium]`, which means `low` and `nit` are advisory.

A repo with high churn and a strong CI suite might tighten this to `[critical, high]`, treating mediums as advisory because the test suite catches them anyway. A repo shipping firmware where a panic is a customer-visible crash might widen the practical interpretation by tuning `review.repo_emphasis` to mark certain patterns as critical regardless of severity scale defaults.

The point of leaving this configurable: severity is not a property of code, it is a property of code in context. Same finding, different repos, different appropriate response. Momus does not pretend to know your context; it gives you the knob.

See `../reference/config-schema.md#review-blocking-severities` and `../how-to/configure-blocking-severities.md`.

## Verify cannot promote

Phase 3 can do four things to a phase 2 finding: drop it, demote its severity, strip its suggestion, or consolidate it with another finding. It cannot raise severity. It cannot add a new finding.

This asymmetry is intentional and load-bearing.

The reasoning: phase 2 has full read access to the worktree, runs first, and is the broadest LLM call in the pipeline. It is the layer where most real findings will originate, and it is also the layer where most hallucinations will originate. Phase 3 exists to **contain** phase 2's errors, not to extend phase 2's reach. If phase 3 could promote, a hallucinating phase 3 could turn a phase 2 nit into a phase 3 critical and block merge on garbage. If phase 3 could add, a prompt-injected phase 3 could invent findings that phase 2 never saw.

Bounding phase 3 to drop / demote / strip / consolidate makes the worst case "we missed something we should have caught" rather than "we blocked merge on something fake". The first failure mode is recoverable on the next push. The second corrodes trust in the bot.

This is the same shape of containment that puts phase 4 outside the LLM entirely. The pattern across the pipeline is consistent: each downstream phase has strictly less authority than the upstream one, even when the downstream phase is also an LLM.

See `momus/prompts/phase3-verify.md` for the prompt that enforces this.

## First-review APPROVE policy {#first-review-approve}

The very first time momus reviews a PR (no prior bot reviews exist), it has a choice: should an empty findings list become an `APPROVE` verdict, or just a `COMMENT`?

`post.first_review_approve_policy` decides. Three modes:

- **`never`** (default) — Never post `APPROVE` on the first review. Verdict is `COMMENT` even with zero findings. This is the styleseat default; it treats APPROVE as a deliberate sign-off, not a default for "I didn't find anything".
- **`if_no_findings`** — `APPROVE` only when the findings list is completely empty. Any finding, even a nit, downgrades to `COMMENT` or `REQUEST_CHANGES`.
- **`if_no_blocking`** — `APPROVE` when no findings hit `blocking_severities`. Nits and lows are fine; a medium would trip it.

Which is right depends on what the bot's APPROVE means in your repo's process. If a green bot review is a load-bearing signal that lets a PR merge, `never` keeps the human in the loop; humans approve, the bot only objects. If the bot is one of several reviewers and an `APPROVE` just clears its lane, `if_no_blocking` is reasonable.

The companion knob is `post.allow_human_approve_override`, which decides whether a human comment containing `lgtm` or `@bot approve` overrides blocking findings. Default is `false`. Both settings are about how much weight you want the bot's verdict to carry.

See `../reference/config-schema.md#post-first-review-approve-policy`.

## Calibration {#calibration}

Calibration is momus's name for the discipline of forcing the LLM to rate its own confidence in a finding before that finding is allowed to block merge. The bot does not just emit a severity; it emits a one-line justification answering "would a human reviewer genuinely block this PR over this?" alongside it. The justification is structured (`would_human_block: yes|no`, `rationale: ...`) and travels with the finding through phase 3.

The model is instructed to demote any finding whose honest answer is "no" or "not sure". The phase 2 prompt makes the rule explicit and the phase 3 verifier audits unconfident findings harder before letting them stand: a finding marked `would_human_block: no` that still claims a blocking severity is a calibration failure and a candidate for demotion or drop.

This is controlled by the `review.require_calibration` config flag (default `true`). When enabled, the renderer inserts a `<<CALIBRATION_PROCEDURE>>` paragraph into the phase 2 prompt and a `<<CALIBRATION_FIELD>>` JSON fragment into the finding schema example. Both substitutions live in `momus/render.py` (see the `require_calibration` branch). When disabled, both tokens render as empty strings, the prompt drops the calibration step, and the schema treats `calibration` as optional.

The mechanism is simple; the effect is leverage. Asking the model to predict whether a human would block produces a different distribution of severities than asking the model to assign a severity directly, because the second framing rewards looking thorough and the first framing rewards being right. See `momus/prompts/phase2-review.md` for the exact prompt site and `../reference/prompt-tokens.md` for the full token list.
