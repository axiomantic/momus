# Conventions

- Token and signature comparisons MUST use `hmac.compare_digest` to
  prevent timing-side-channel byte recovery.
- Bytes/bytestring comparison with `==` short-circuits and leaks length
  information; never use it for credential comparison.
