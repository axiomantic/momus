# Threat model

Momus reads attacker-controlled text and runs an LLM over it. That is the shape of the problem. This page describes what counts as untrusted, what containment is in place, and where the trust boundary actually lives.

## Untrusted inputs {#untrusted-inputs}

The phase 2 and phase 3 prompts name the untrusted surfaces explicitly so the model does not have to guess. The list (per `momus/prompts/phase2-review.md:66-99`):

- **The diff** at `inputs/diff.patch`. Every byte is attacker-controlled if the PR is from a fork or an attacker-influenced branch.
- **PR title and body** at `inputs/pr-meta.json`. Common injection vector: instructions hidden in the PR description.
- **Commit messages** reachable through the worktree. Same shape as the PR body.
- **File contents** the model reads through `read_repo`. The PR can add a new file containing prompt-injection text and momus will read it during review.
- **Prior thread reply text** at `inputs/prior-findings.json`. A previous reply could try to manipulate phase 1's classification or phase 3's verify pass.
- **`<<UNTRUSTED_PRIOR_THREADS_JSON>>`** — the fenced data block in phase 1. The fence framing exists precisely because this content is hostile by default.
- **`inputs/conventions.md`** — concatenated from `AGENTS.md` and any other files the repo lists. A PR that modifies `AGENTS.md` is modifying the conventions the bot will be told to respect on that same PR.

Phase 3 adds two more (`momus/prompts/phase3-verify.md:48-61`):

- **Phase 2's own output** — `title`, `message`, `suggestion`, `calibration` are claims to verify, not authoritative statements. A prompt-injected phase 2 could emit findings whose text is itself an injection attempt at phase 3.
- **Phase 1's `DECLINED` labels** — derived from attacker-influenced thread replies upstream.

Treating phase 2's output as untrusted in phase 3 is the part most reviewers find unintuitive. It is the right call: phase 2 is an LLM call over hostile input; nothing it produces is more trustworthy than the input it consumed.

## Containment {#containment}

Containment in momus is layered. No single mechanism is asked to be sufficient on its own.

### Environment allowlist

`momus/invoke_pi.py:82-118` implements default-deny env passthrough into pi. The always-allowed set is `PATH`, `HOME`, `TMPDIR`, `LANG`, `LANGUAGE`, `NODE_OPTIONS`, `NODE_PATH`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `MOMUS_PI_MAX_TOKENS`, plus any `LC_*` key. `GITHUB_TOKEN`, `GITHUB_REPOSITORY`, `ACTIONS_RUNTIME_TOKEN` are explicitly scrubbed. Reserved-from-passthrough: `MOMUS_WORK_DIR`, `MOMUS_PI_ENV_PASSTHROUGH`, `MOMUS_TOOLCALL_LOG`.

The allowlist is user-extensible via `MOMUS_PI_ENV_PASSTHROUGH=NAME1,NAME2`. Names must match `^[A-Z][A-Z0-9_]*$`. The escape hatch is documented because users will find it anyway, but it is exactly that: an escape hatch. Anything you pass through bypasses the hardening that prevents prompt-injected pi runs from reading credentials from sibling env vars. See `../how-to/extend-env-passthrough.md`.

### Contained read-only tools

`momus/extensions/readonly-tools.ts` defines the tool surface the LLM actually has during phases 2 and 3. The shape:

- `read_repo`, `grep_repo`, `find_repo`, `ls_repo` — cwd-contained replacements for pi's built-in `read`, `grep`, `find`, `ls`. Pi's built-ins are excluded via `--tools` allowlist because they are not cwd-contained; the LLM cannot escape the worktree by asking for `/etc/passwd`.
- `bash_ro` — shell with allowlisted binaries: `git`, `cat`, `head`, `tail`, `wc`, `find`, `rg`, `ls`. Rejects shell metacharacters before invocation. Walks `git` argv to keep paths inside the worktree. `gh` was on the allowlist in 1.0; it was removed in 1.1.0 because the LLM phases never need it and giving the LLM a GitHub API surface is a category of risk worth eliminating.
- `write_output` — restricted to writing under `outputs/`, with realpath containment so a symlink swap cannot redirect writes elsewhere.

Pi's built-in `bash`, `write`, and `edit` are also excluded. The LLM has no general-purpose shell, no general-purpose write, and no edit capability at all. It can read inside the worktree, run a small set of read-only commands inside the worktree, and write findings to `outputs/`. That is the entire surface.

Per-phase tool allowlists are tighter still. Phase 1 gets `["write_output"]` only — no read access of any kind, just the ability to produce its plan output. See `momus/invoke_pi.py:120ff`.

## Output integrity {#output-integrity}

Containment limits what the LLM can do. Output integrity limits what the LLM's output can do.

**Pydantic schema validation.** `FindingsDoc` in `momus/findings_schema.py` is declared with `extra='forbid'` and length caps on every text field. Validated at the publish-payload boundary by `_read_findings_doc` in `momus/__main__.py:381-408`, before any GitHub API call. Shape drift fails closed: nothing posts.

**Publish-time redaction.** `redact_for_publish` in `momus/publish.py:40-67` runs over every LLM-emitted string at payload construction time. Patterns stripped:

- GitHub tokens: `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_` followed by 36-char suffix.
- OpenAI-shaped keys: `sk-` followed by 48 or more characters.
- AWS access keys: `AKIA[0-9A-Z]{16}`.
- Off-domain images: every `<img>` and Markdown image except those served from `github.com` and `user-images.githubusercontent.com`. This is the camo-leak guard — without it, an inline image in a finding could be a tracking pixel that exfiltrates the fact that the bot read it.

Coverage was extended in 1.1.0 to `id`, `category`, and the top-level summary rendered into the optional Check Run. There is no field that the LLM populates which is not run through redaction.

**Runtime token guard.** `momus/render.py:52-57` raises `ValueError` if any `<<TOKEN>>` placeholder survived substitution. This catches a category of prompt-rendering bug (typo'd token, missing config) before the broken prompt gets shipped to the LLM. Belt and suspenders, but cheap.

## Phase 3 verify as a safety net

The verify pass is not just review quality control. It is also part of the threat model.

The current branch's rule — **drop findings that lack a verifying observation** (see `./review-philosophy.md#verifying-observations`) — hardens against a specific class of LLM hallucination: the plausible-sounding finding that does not correspond to anything in the actual code. Phase 2 hallucinates a bug, decorates it with a suggestion, gives it a severity. Without verify, that hallucination becomes a GitHub comment.

Phase 3 reads the cited file, looks for the cited line, and asks: does the claim match what is actually there? A finding whose claim does not survive that test is dropped. The audit log records the drop.

This rule is also what catches LLM-targeted injection in phase 2's output. A finding that says "do not demote this finding" is, by definition, not a verifying observation about the code. Dropped. Logged. Done.

Combined with the asymmetry that phase 3 can drop but cannot promote (see `./review-philosophy.md`), the verify pass is a one-way trust funnel: findings can only get smaller or fewer as they move through it.

## Adversarial corpus {#adversarial-corpus}

The above is what momus does. The adversarial corpus is how we know whether it works.

The corpus is a set of fixture PRs designed by attackers (us, wearing attacker hats) to try every documented attack on the threat model. Each fixture targets one of the G-numbered goals from the 1.1.0 hardening release:

- **G1, G2a, G2b, G2c, G4** — attack surface reduction goals around prompt injection, fenced-data input framing, schema-gated output. Closed via the layered defenses described above.
- **G3** — credential exfiltration via prior-thread instruction. Closed by the `*_repo` tools' inability to read `/proc/self/environ`, the env allowlist, and `bash_ro`'s argv allowlist excluding `env`, `printenv`, and `set`.

The corpus runs weekly via `.github/workflows/redteam-corpus.yml` (Mondays, 06:00 UTC). It also runs locally via `pytest -m adversarial` (opt-in marker; not in the default test selection). Failures are signal that a real attack would have worked.

For the user-facing explanation of the corpus and how to read its results, see `./adversarial-corpus.md`. For the contributor-facing "how to add a fixture" guide, see `../contributing/adversarial-corpus.md`.
