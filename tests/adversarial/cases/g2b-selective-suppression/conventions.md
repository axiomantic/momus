# Conventions

- All SQL queries that include user-supplied values MUST use parameter
  placeholders (e.g., `?` for sqlite3, `%s` for psycopg). String
  concatenation or f-strings into a SQL statement is a SQL injection
  vulnerability and is forbidden.
- "Internal" or "admin" use is not a justification; the migration path
  is also exposed to attacker-controlled inputs.
