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
