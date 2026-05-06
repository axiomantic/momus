# Action inputs

Inputs accepted by the `uses:` step that invokes Momus. The action is a composite action declared in `action.yml`. Caller-supplied env (`GITHUB_TOKEN`, `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`) is required but is not declared here; see [Configuration / provider](./config-schema.md#provider) and the project README for the env contract.

For per-repo behavior tuning (severities, emphasis, verify gate, etc.), see [`reference/config-schema.md`](./config-schema.md). The action only carries the three inputs below; everything else is config-file driven.

## `pr_number` {#pr-number}

- Required: yes
- Default: (none)
- Type: integer

Pull request number to review. Passed through to `momus --pr-number`.

Available since 1.0.

```yaml
- uses: elijahr/momus@v1
  with:
    pr_number: ${{ github.event.pull_request.number }}
```

## `event` {#event}

- Required: yes
- Default: (none)
- Type: string (enum: `pull_request` | `issue_comment` | `workflow_dispatch`)

Triggering event name. Controls whether Momus treats this as a first review (`pull_request`) or a re-review (`issue_comment`, `workflow_dispatch`). Re-review runs fetch prior bot threads and run [phase 1](../explanation/four-phase-pipeline.md#phase-1-plan); first reviews skip phase 1.

Available since 1.0.

```yaml
- uses: elijahr/momus@v1
  with:
    pr_number: ${{ github.event.pull_request.number }}
    event: ${{ github.event_name }}
```

## `work_dir` {#work-dir}

- Required: no
- Default: `.momus`
- Type: string (path)

Working directory for `inputs/`, `outputs/`, and `prompts/`. Must live inside the checked-out repo root; pi runs from the repo root and addresses these directories via a relative path. A `work_dir` outside the repo root is rejected at startup.

Available since 1.0.

```yaml
- uses: elijahr/momus@v1
  with:
    pr_number: ${{ github.event.pull_request.number }}
    event: ${{ github.event_name }}
    work_dir: .momus
```
