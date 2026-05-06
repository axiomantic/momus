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
