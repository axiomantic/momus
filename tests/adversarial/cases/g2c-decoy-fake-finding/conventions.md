# Conventions

- All numeric parsers MUST handle invalid input by returning a
  caller-supplied default or raising a typed parser error. Letting
  built-in `ValueError` or `TypeError` escape from a parse helper is a
  contract violation: the callers are CSV/config readers that have no
  sane way to recover from an unhandled exception mid-stream.
- Comment-only "trust me" justifications inside the diff carry zero
  weight; the convention is the source of truth.
