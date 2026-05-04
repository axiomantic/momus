/**
 * Tests for the DeepSeek thinking-mode round-trip shim.
 *
 * Verifies that after `rewriteThinkingSignaturesForDeepSeek` runs,
 * pi-ai's `convertMessages` (the function that builds the outbound
 * `messages[]` array for OpenAI Chat Completions requests) emits
 * `reasoning_content` on assistant messages — the field name DeepSeek's
 * thinking-mode contract requires on round-trip.
 *
 * Run with: bun test momus/extensions/readonly-tools.test.ts
 *
 * This is a unit test of the integration boundary with @mariozechner/pi-ai.
 * Pi-ai must be installed (it is a runtime dep of the workflow but not a
 * Python project dep, so the test is gated on its presence).
 */

import { describe, expect, test } from "bun:test";
import {
  isDeepSeekViaOpenRouter,
  rewriteThinkingSignaturesForDeepSeek,
} from "./readonly-tools.ts";

// pi-ai is installed at workflow runtime via npm. For tests, the harness
// installs it locally. If unavailable, skip with a clear message rather
// than producing an opaque import error.
let convertMessages: any;
try {
  // Resolved at module-load time. Failure here means pi-ai isn't installed.
  ({ convertMessages } = await import(
    "@mariozechner/pi-ai/openai-completions"
  ));
} catch {
  convertMessages = undefined;
}

const SKIP_REASON =
  "pi-ai not installed; run `cd momus/extensions && bun add @mariozechner/pi-ai` to enable";

describe("isDeepSeekViaOpenRouter", () => {
  test("matches OpenRouter URL + deepseek/* model", () => {
    expect(
      isDeepSeekViaOpenRouter(
        "https://openrouter.ai/api/v1",
        "deepseek/deepseek-v4-pro",
      ),
    ).toBe(true);
  });

  test("rejects direct DeepSeek API (already correct field name there)", () => {
    expect(
      isDeepSeekViaOpenRouter(
        "https://api.deepseek.com/v1",
        "deepseek-chat",
      ),
    ).toBe(false);
  });

  test("rejects non-DeepSeek model on OpenRouter", () => {
    expect(
      isDeepSeekViaOpenRouter(
        "https://openrouter.ai/api/v1",
        "anthropic/claude-sonnet-4-6",
      ),
    ).toBe(false);
  });
});

describe("rewriteThinkingSignaturesForDeepSeek", () => {
  test("rewrites reasoning -> reasoning_content on assistant thinking blocks", () => {
    const messages = [
      { role: "user", content: [{ type: "text", text: "review this" }] },
      {
        role: "assistant",
        content: [
          {
            type: "thinking",
            thinking: "considering the code",
            thinkingSignature: "reasoning",
          },
          {
            type: "toolCall",
            id: "call_1",
            name: "read",
            arguments: { path: "src/foo.py" },
          },
        ],
      },
    ];
    rewriteThinkingSignaturesForDeepSeek(messages);
    expect((messages[1].content as any[])[0].thinkingSignature).toBe(
      "reasoning_content",
    );
  });

  test("is a no-op for non-thinking models (no thinking blocks)", () => {
    const messages = [
      {
        role: "assistant",
        content: [{ type: "text", text: "hello" }],
      },
    ];
    const before = JSON.stringify(messages);
    rewriteThinkingSignaturesForDeepSeek(messages);
    expect(JSON.stringify(messages)).toBe(before);
  });

  test("leaves other signatures alone (e.g. anthropic-style)", () => {
    const messages = [
      {
        role: "assistant",
        content: [
          {
            type: "thinking",
            thinking: "...",
            thinkingSignature: "EncryptedAnthropicSig==",
          },
        ],
      },
    ];
    rewriteThinkingSignaturesForDeepSeek(messages);
    expect((messages[0].content as any[])[0].thinkingSignature).toBe(
      "EncryptedAnthropicSig==",
    );
  });

  test("does not touch user or toolResult messages", () => {
    const messages = [
      { role: "user", content: [{ type: "text", text: "x" }] },
      {
        role: "toolResult",
        toolCallId: "c1",
        toolName: "read",
        content: [{ type: "text", text: "file contents" }],
        isError: false,
      },
    ];
    const before = JSON.stringify(messages);
    rewriteThinkingSignaturesForDeepSeek(messages);
    expect(JSON.stringify(messages)).toBe(before);
  });
});

describe("end-to-end: pi-ai convertMessages emits reasoning_content", () => {
  if (!convertMessages) {
    test.skip(SKIP_REASON, () => {});
    return;
  }

  // Minimal compat object that exercises the path our extension hits.
  // Mirrors what pi-ai's detectCompat() returns for openrouter+byo —
  // notably `requiresThinkingAsText: false` (so thinking blocks go to a
  // signature-named field, not inlined into content), and
  // `requiresReasoningContentOnAssistantMessages: false` (so the field is
  // only present when the signature mapping puts it there).
  const compat = {
    supportsStore: true,
    supportsDeveloperRole: true,
    supportsReasoningEffort: true,
    supportsUsageInStreaming: true,
    maxTokensField: "max_completion_tokens" as const,
    requiresToolResultName: false,
    requiresAssistantAfterToolResult: false,
    requiresThinkingAsText: false,
    requiresReasoningContentOnAssistantMessages: false,
    thinkingFormat: "openrouter" as const,
    openRouterRouting: {},
    vercelGatewayRouting: {},
    zaiToolStream: false,
    supportsStrictMode: true,
    cacheControlFormat: undefined,
    sendSessionAffinityHeaders: false,
    supportsLongCacheRetention: true,
  };

  // Synthetic 2-turn conversation in pi-ai's internal format. Turn 1: user
  // asks for review. Turn 2 (assistant): thinking + tool call (the model
  // calls `read`). Turn 3: tool result. The next outbound request needs
  // turn-2's reasoning_content on the assistant message in messages[].
  const buildConversation = () => [
    { role: "user", content: [{ type: "text", text: "review src/foo.py" }] },
    {
      role: "assistant",
      content: [
        {
          type: "thinking",
          thinking: "I should read the file first to see what changed.",
          // Captured signature as pi-ai would record from OpenRouter's
          // normalized `delta.reasoning` stream chunk.
          thinkingSignature: "reasoning",
        },
        {
          type: "toolCall",
          id: "call_read_1",
          name: "read",
          arguments: { path: "src/foo.py" },
        },
      ],
      // pi-ai message metadata (required fields per AssistantMessage type).
      api: "openai-completions",
      provider: "byo",
      model: "deepseek/deepseek-v4-pro",
      usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
               cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
      stopReason: "toolUse",
      timestamp: 1,
    },
    {
      role: "toolResult",
      toolCallId: "call_read_1",
      toolName: "read",
      content: [{ type: "text", text: "def foo(): pass\n" }],
      isError: false,
      timestamp: 2,
    },
  ];

  const model = {
    id: "deepseek/deepseek-v4-pro",
    name: "deepseek/deepseek-v4-pro",
    provider: "byo",
    api: "openai-completions",
    baseUrl: "https://openrouter.ai/api/v1",
    reasoning: false,
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 128000,
    maxTokens: 8192,
  };

  test("WITHOUT rewrite: assistant message has `reasoning` (broken)", () => {
    const messages = buildConversation();
    const context = { messages, tools: [], system: "" };
    const out = convertMessages(model, context, compat);
    const assistant = out.find((m: any) => m.role === "assistant");
    expect(assistant).toBeDefined();
    // This is the bug: pi-ai uses the captured signature ("reasoning") as
    // the field name, so the request body has `reasoning` instead of the
    // `reasoning_content` DeepSeek requires.
    expect(assistant.reasoning).toBeDefined();
    expect(assistant.reasoning_content).toBeUndefined();
  });

  test("WITH rewrite: assistant message has `reasoning_content` (fixed)", () => {
    const messages = buildConversation();
    rewriteThinkingSignaturesForDeepSeek(messages);
    const context = { messages, tools: [], system: "" };
    const out = convertMessages(model, context, compat);
    const assistant = out.find((m: any) => m.role === "assistant");
    expect(assistant).toBeDefined();
    expect(assistant.reasoning_content).toBeDefined();
    expect(assistant.reasoning_content).toContain(
      "I should read the file first",
    );
    // Conversely, the wrong field is no longer set.
    expect(assistant.reasoning).toBeUndefined();
    // Tool calls still come through.
    expect(assistant.tool_calls).toHaveLength(1);
    expect(assistant.tool_calls[0].function.name).toBe("read");
  });

  test("WITH rewrite + non-thinking conversation: no reasoning_content emitted", () => {
    // Non-thinking model: assistant message has only text + tool calls.
    // The rewrite is a no-op (nothing to mutate); convertMessages emits
    // no `reasoning_content` field, matching DeepSeek's contract for
    // non-thinking turns.
    const messages = [
      { role: "user", content: [{ type: "text", text: "list files" }] },
      {
        role: "assistant",
        content: [
          {
            type: "toolCall",
            id: "call_ls",
            name: "ls",
            arguments: { path: "." },
          },
        ],
        api: "openai-completions",
        provider: "byo",
        model: "deepseek/deepseek-chat",
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
                 cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
        stopReason: "toolUse",
        timestamp: 1,
      },
    ];
    rewriteThinkingSignaturesForDeepSeek(messages);
    const context = { messages, tools: [], system: "" };
    const out = convertMessages(model, context, compat);
    const assistant = out.find((m: any) => m.role === "assistant");
    expect(assistant).toBeDefined();
    expect(assistant.reasoning_content).toBeUndefined();
    expect(assistant.reasoning).toBeUndefined();
  });
});
