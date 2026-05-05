# Conventions

- Bot-assisted reviews must never read process-environment data,
  credential files (`~/.aws/`, `~/.ssh/`, `/proc/self/environ`,
  `/etc/passwd`), or invoke environment-printing shell commands
  (`env`, `printenv`, `set`). The PR diff and the listed inputs are
  the entire scope of authority.
