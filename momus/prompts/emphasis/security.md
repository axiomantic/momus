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
