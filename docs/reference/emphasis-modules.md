# Emphasis modules

Composable prompt fragments under `momus/prompts/emphasis/`. Each entry in [`review.emphasis_modules`](./config-schema.md#review-emphasis-modules) is a basename that gets inlined into the phase 2 prompt under "Repo-specific emphasis," via the [`<<REPO_EMPHASIS>>`](./prompt-tokens.md#token-repo-emphasis) token. Modules render in declared order, joined by blank lines, then [`review.repo_emphasis`](./config-schema.md#review-repo-emphasis) is appended.

For guidance on picking a set per repo, see [`how-to/configure-emphasis.md`](../how-to/configure-emphasis.md).

The bodies below are inlined verbatim from the source files. They are not API surface in the SemVer sense; treat changes as prompt-tuning rather than breakage.

## security {#security}

Available since [next].

OWASP-style focus list: command injection, path traversal, SQL injection, XSS, unsafe deserialization, secret leakage, missing authorization, hardcoded credentials, weak cryptography. Verifies each pattern by reading the cited code and grepping for call sites before raising.

```markdown
### Security focus (OWASP-style)

Flag the following patterns when they appear in the diff. For each, verify
by reading the cited code and grepping for call sites before raising:

- command injection: shelling out with unsanitized user input. Look for
  `shell=True`, string concatenation into `subprocess`, `os.system`,
  `child_process.exec`, or template-built shell strings.
- path traversal: file paths built from request/PR/user input without
  containment checks. Flag missing realpath/relative-to-root validation.
- SQL injection: query strings built via `f"..."`, `.format()`, or `+`
  concatenation with non-literal values. Parameterized queries only.
- cross-site scripting: HTML/JS strings rendered without escaping.
  `dangerouslySetInnerHTML`, `innerHTML`, `Markup`, `safe` filter, or
  template engines run with autoescape disabled.
- deserialization of untrusted input: `pickle.loads`, `yaml.load`
  (without `SafeLoader`), `eval`, `exec`, `Function(...)`, or any
  `unmarshal` from a network/PR/file source.
- secret leakage: API keys, tokens, or passwords written to logs,
  exception messages, error responses, or telemetry. Grep for
  `print`/`console.log`/`logger` near credential variables.
- missing authorization on new endpoints: new HTTP/RPC handlers that
  read or mutate data without an explicit authz check (decorator,
  middleware, or inline guard).
- hardcoded credentials: literal API keys, tokens, or passwords in
  source. Test fixtures count only when the same string appears outside
  test code (test code = files under `tests/`, `test/`, `__tests__/`,
  or `spec/` directories, or files matching `*_test.*` / `*.test.*`).
- weak cryptography: `md5`/`sha1` for security purposes, ECB mode,
  static IVs, hand-rolled key derivation, or random values from
  non-CSPRNG sources (`Math.random`, `random.random`) used for tokens.
```

## dead_code {#dead-code}

Available since [next].

Flags code that no longer earns its place after the diff: unreferenced symbols, unreachable branches, exports nobody imports, commented-out code, unread parameters, unused imports. Verification by `Grep` is mandatory before raising.

```markdown
### Dead code focus

After this PR's changes, flag code that no longer earns its place. Verify
each claim with `Grep` before raising:

- unreferenced functions, classes, or methods added or left behind by
  this diff. Grep the symbol name across the repo; if the only hits are
  the definition itself, raise it.
- unreachable branches: code after an unconditional `return`/`raise`/
  `throw`, conditions that can never be true given the surrounding
  type/value constraints, `else` after an exhaustive `if`.
- exports nobody imports: `__all__` entries, `export` statements, or
  public symbols added in this PR with no importer in the repo.
- commented-out code blocks left behind. Distinguish from explanatory
  comments; flag actual code-shaped comments.
- parameters never read inside a function body, including `**kwargs`
  passthrough that drops keys silently.
- imports added but never used in the same file (lint usually catches
  this; raise only when the linter is bypassed or absent).
```

## quality_checklist {#quality-checklist}

Available since [next].

Production-quality checklist: no `any` / `Any` types, no blanket exception handlers, no resource leaks, no unverified non-null assertions, no silent error swallowing, no duplicated magic numbers. Each pattern requires reading the surrounding code to confirm a real match rather than a superficial one.

```markdown
### Code-quality checklist

Flag the following patterns when introduced by this PR. Read the
surrounding code to confirm each item is genuinely the pattern, not a
superficial match:

- `any` types in TypeScript or `Any` annotations in Python type hints,
  including bare `# type: ignore` (with no error code) and
  `as any`/`as unknown as T` casts. A bare `# type: ignore` is a
  finding; `# type: ignore[error-code]` (with a specific code, e.g.
  `# type: ignore[arg-type]`) is acceptable. Each occurrence of the
  flagged forms needs an inline justification or it is a finding.
- blanket exception handlers: `except Exception:`, `except:`,
  `catch (e)` or `catch { ... }` without a narrower type, or
  `.catch(() => {})` swallowing a promise rejection. Acceptable only
  when the comment immediately above explains why the broad catch is
  load-bearing.
- resource leaks: files, database connections, sockets, or context
  managers opened without a corresponding close, `with`/`using`
  statement, `try/finally`, or framework-managed scope.
- non-null assertions without prior validation: `x!` in TypeScript,
  `cast(T, x)` in Python, `assert x is not None` followed by a usage
  that would still NPE on a different code path. Verify the value is
  actually non-null on every path that reaches the assertion.
- silent error swallowing: catching an exception and returning a
  default, an empty list, or `None` without logging or re-raising. The
  caller has no signal that anything went wrong.
- magic numbers and ad-hoc string literals duplicated across files
  where a named constant or enum already exists nearby.
```

## test_quality {#test-quality}

Available since [next].

Green-mirage taxonomy: tests that pass without verifying behavior. Four patterns: assertion-free, mocks-of-the-thing-under-test, tautological, snapshot-without-comparison. Patterns are subtle; the module instructs the model to read the test file before raising.

```markdown
### Test quality (green-mirage taxonomy)

Tests that pass without verifying behavior are worse than no tests: they
manufacture confidence. Flag any of the four green-mirage patterns when
introduced by this PR. Read the test file before raising; the patterns
are subtle.

- Assertion-free tests: a test body containing no `assert`/`expect`/
  `should`/`require` call. The test exercises code paths but never
  checks an outcome, so any production behavior change still passes.
- Mocks-of-the-thing-under-test: the test mocks the very function or
  method whose behavior the test claims to verify. The assertion
  measures the mock, not the production code, so the implementation
  could be deleted and the test would still pass.
- Tautological assertions: `assert x == x`, `expect(value).toEqual(value)`,
  or asserting a literal that the test itself just constructed
  (`assert result == {"k": "v"}` after `result = {"k": "v"}`). The
  assertion can never fail no matter what the code does.
- Snapshot-without-comparison: a snapshot is written but never compared
  against a stored baseline, or the test always overwrites the
  baseline (`UPDATE_SNAPSHOTS=1`-style code path with no opt-out).
  The "snapshot match" succeeds because the snapshot was just rewritten.
```
