# Use a different LLM provider

You want to point Momus at a provider other than the OpenRouter + DeepSeek default. Momus speaks the OpenAI Chat Completions wire format, so any provider that exposes that format works. The contract is three values: `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`.

The API key always lives in the `LLM_API_KEY` repo (or org) secret. The model and base URL can be set on the workflow `with:` block (workflow-wide default) or in `.momus.yaml` under [`provider`](../reference/config-schema.md#provider) (per-repo override). Per-repo overrides win when both are set.

## OpenRouter (default)

One key for every major model. Cheapest path to experiment.

- Get a key: https://openrouter.ai/settings/keys
- Base URL: `https://openrouter.ai/api/v1`
- Suggested models:
  - `deepseek/deepseek-v4-flash` (default; strong code review at low cost)
  - `deepseek/deepseek-v4-pro`
  - `anthropic/claude-sonnet-4-6`
  - `openai/gpt-4o`
  - `google/gemini-2.0-flash` (cheapest)

Workflow override: leave defaults, just set `LLM_API_KEY`.

## Anthropic Claude (direct)

Direct billing through Anthropic.

- Get a key: https://console.anthropic.com/settings/keys
- Base URL: `https://api.anthropic.com/v1`
- Suggested models: `claude-sonnet-4-6`, `claude-opus-4-7`, `claude-haiku-4-5-20251001`

```yaml
# .momus.yaml
provider:
  model: claude-sonnet-4-6
  base_url: https://api.anthropic.com/v1
```

## OpenAI

Direct billing through OpenAI.

- Get a key: https://platform.openai.com/api-keys
- Base URL: `https://api.openai.com/v1`
- Suggested models: `gpt-4o`, `gpt-4-turbo`, `o1`, `o3-mini`

```yaml
provider:
  model: gpt-4o
  base_url: https://api.openai.com/v1
```

## Google Gemini

- Get a key: https://aistudio.google.com/app/apikey
- Base URL: `https://generativelanguage.googleapis.com/v1beta/openai/`
- Suggested models: `gemini-2.0-flash`, `gemini-2.5-pro`

```yaml
provider:
  model: gemini-2.0-flash
  base_url: https://generativelanguage.googleapis.com/v1beta/openai/
```

## DeepSeek (direct)

DeepSeek without OpenRouter.

- Get a key: https://platform.deepseek.com/api_keys
- Base URL: `https://api.deepseek.com/v1`
- Suggested models: `deepseek-v4-flash`, `deepseek-v4-pro`

```yaml
provider:
  model: deepseek-v4-flash
  base_url: https://api.deepseek.com/v1
```

## Bedrock, Together, Groq, Mistral, others

Any OpenAI-compatible endpoint works. Set `base_url` to the provider's `/v1` URL and use whatever model slug they publish. For Bedrock, the typical path is via LiteLLM (or any other proxy) that exposes the OpenAI shape.

```yaml
provider:
  model: <provider-model-slug>
  base_url: <provider-v1-url>
```

The matching `LLM_API_KEY` secret still goes through the workflow.

## Workflow-wide vs per-repo {#per-repo-override}

Two places set the same values:

| Where | Use when |
|---|---|
| Workflow `with:` block | The default provider for every repo invoking this workflow |
| `.momus.yaml` `provider:` | This repo needs something different from the workflow default |

The per-repo `.momus.yaml` value wins. An empty string in `.momus.yaml` means "inherit from the workflow."

## See also

- [Reference: `provider.model`](../reference/config-schema.md#provider-model)
- [Reference: `provider.base_url`](../reference/config-schema.md#provider-base-url)
- [How-to: tune cost vs thoroughness](./tune-cost-vs-thoroughness.md) (picking a smaller model)
- [How-to: set up the GitHub App](./set-up-github-app.md)
