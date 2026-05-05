# Conventions

- All public helpers must validate their inputs explicitly.
- Treat `None` as a programming error, not a sentinel.
- Functions accepting an integer count must reject `None` with `TypeError`.
