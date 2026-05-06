# Extend the pi env passthrough

You want a custom env var inside pi (the LLM phases). Momus runs pi with a default-deny env allowlist, so anything outside the allowed set is scrubbed. The escape hatch is `MOMUS_PI_ENV_PASSTHROUGH`.

**Read [Threat model / containment](../explanation/threat-model.md#containment) first.** Each variable you pass through bypasses the hardening that prevents prompt-injected pi runs from reading credentials from sibling env vars.

## Default allowlist

Always allowed (`momus/invoke_pi.py:82-118`):

- `PATH`, `HOME`, `TMPDIR`
- `LANG`, `LANGUAGE`, plus any `LC_*` key
- `NODE_OPTIONS`, `NODE_PATH`
- `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`

Always scrubbed: `GITHUB_TOKEN`, `GITHUB_REPOSITORY`, `ACTIONS_RUNTIME_TOKEN`. Anything else not on the allowlist is dropped before pi starts.

## Reserved names

`MOMUS_WORK_DIR`, `MOMUS_PI_ENV_PASSTHROUGH`, and `MOMUS_TOOLCALL_LOG` are reserved and cannot be added via the passthrough mechanism. They are Momus's own runtime channel into pi; forwarding them as user-controlled values would let a fork override Momus's own contract.

## Add a custom var

Set `MOMUS_PI_ENV_PASSTHROUGH=NAME1,NAME2` on the workflow job. Each name is added to the allowlist and forwarded as-is.

```yaml
jobs:
  call:
    uses: axiomantic/.github/.github/workflows/momus.yml@devel
    env:
      MOMUS_PI_ENV_PASSTHROUGH: MY_CUSTOM_VAR,ANOTHER_VAR
      MY_CUSTOM_VAR: ${{ secrets.MY_CUSTOM_VAR }}
      ANOTHER_VAR: some-value
    with:
      pr_number: ${{ github.event.pull_request.number }}
      event_name: ${{ github.event_name }}
    secrets:
      LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
```

## Name shape

Each name must match `^[A-Z][A-Z0-9_]*$`. Lowercase names, hyphens, and leading digits are rejected at startup. The regex exists to prevent accidental shell-metacharacter injection through the comma-separated list.

The `LC_*` prefix rule is separate: any variable starting with `LC_` is allowed without listing it. This is for locale settings the runner injects automatically; you do not need to list `LC_ALL`, `LC_CTYPE`, etc.

## What "passing through" actually means

The variable is read from the runner environment and exported into the pi child process unchanged. The LLM tools running inside pi can then read it via whatever mechanism they use (env in `bash_ro`, etc.).

Pi's `bash_ro` allowlist still applies; the LLM cannot run `env`, `printenv`, or `set` to enumerate the environment. But anything you forward is reachable to a tool that knows the variable's name.

## When to use this

Reasonable uses:
- A custom telemetry endpoint URL for a fork's instrumented pi tools.
- A feature flag for an extension you wrote and audited.
- A cache directory path your extension reads.

Unreasonable uses:
- Forwarding `GITHUB_TOKEN` "because the LLM might need GitHub access" (it does not; phase 4 is the only GitHub-talking part of Momus, and it is pure Python).
- Forwarding any credential you would not be willing to embed in a prompt-injection payload's response.

## See also

- [Explanation: threat model / containment](../explanation/threat-model.md#containment)
- [Reference: CLI / environment](../reference/cli.md#environment)
- [Reference: prompt tokens](../reference/prompt-tokens.md) (for the in-prompt untrusted-input plumbing this is the env-side counterpart of)
