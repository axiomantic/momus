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
import { clampThinkingLevel, getModels, getProviders } from "@mariozechner/pi-ai";
import { mkdirSync, mkdtempSync, realpathSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  checkGitArgv,
  ensureWithinCwd,
  executeFindRepo,
  executeGrepRepo,
  executeLsRepo,
  executeReadRepo,
  executeWriteOutput,
  buildByoModelEntry,
  isDeepSeekViaOpenRouter,
  lookupModelCost,
  lookupModelLimits,
  lookupModelReasoning,
  rejectAbsoluteArgv,
  resolveMaxTokens,
  rewriteThinkingSignaturesForDeepSeek,
  validateMomusWorkDir,
} from "./readonly-tools.ts";

function makeCwd(): string {
  // realpathSync to resolve macOS /var -> /private/var so the helper's
  // realpath comparisons line up.
  return realpathSync(mkdtempSync(join(tmpdir(), "momus-rotest-")));
}

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

  // A NON-thinking compat/model pair, used to isolate the rewrite helper
  // from the thinking-mode wire settings. It is deliberately not what momus
  // registers: `requiresReasoningContentOnAssistantMessages: false` and
  // `reasoning: false` between them disable pi-ai's reasoning_content stub,
  // so these tests observe the rewrite helper and nothing else. The
  // registered pair is covered separately below.
  // `requiresThinkingAsText: false` keeps thinking blocks in a
  // signature-named field rather than inlined into content, which is the
  // behaviour the rewrite helper acts on.
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

  test("WITH rewrite + non-thinking model/compat: rewrite adds no reasoning_content", () => {
    // Non-thinking model: assistant message has only text + tool calls.
    // The rewrite is a no-op (nothing to mutate), and with reasoning:false
    // pi-ai's reasoning_content stub is gated off, so convertMessages emits
    // no `reasoning_content` field. This pins the rewrite helper's
    // no-op property; it is NOT a claim about momus's registered model,
    // which does enable reasoning (see the next test).
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

  test("REGISTERED entry + non-thinking turn: emits the reasoning_content stub", () => {
    // The same non-thinking conversation as above, but run against the model
    // entry the extension actually registers and the compat the registry
    // ships with it. DeepSeek's thinking mode requires reasoning_content on
    // every replayed assistant message, including turns that produced no
    // thinking block; pi-ai supplies an empty string for those. Without this
    // stub the next turn is rejected with error 20015.
    const registered = buildByoModelEntry("deepseek/deepseek-v4-pro");
    // getCompat() is not exported, but it resolves each field as
    // `model.compat.<field> ?? detected.<field>`, which for an override
    // object with only defined keys is a plain spread over the detected base.
    const resolvedCompat = { ...compat, ...registered.compat };
    const messages = [
      { role: "user", content: [{ type: "text", text: "list files" }] },
      {
        role: "assistant",
        content: [
          { type: "toolCall", id: "call_ls", name: "ls_repo", arguments: { path: "." } },
        ],
        api: "openai-completions",
        provider: "byo",
        model: "deepseek/deepseek-v4-pro",
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
                 cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
        stopReason: "toolUse",
        timestamp: 1,
      },
    ];
    rewriteThinkingSignaturesForDeepSeek(messages);
    const context = { messages, tools: [], system: "" };
    const out = convertMessages(registered, context, resolvedCompat);
    const assistant = out.find((m: any) => m.role === "assistant");
    expect(assistant).toBeDefined();
    expect(assistant.reasoning_content).toBe("");
    // The wrong field name is still never used.
    expect(assistant.reasoning).toBeUndefined();
    expect(assistant.tool_calls).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// W2-Tools-Read: read_repo containment + behavior
// ---------------------------------------------------------------------------

describe("read_repo", () => {
  test("read_repo_accepts_relative_path_under_cwd", async () => {
    const cwd = makeCwd();
    writeFileSync(join(cwd, "hello.txt"), "line1\nline2\nline3\n");
    const result = await executeReadRepo({ path: "hello.txt" }, cwd);
    expect(result.isError).toBeUndefined();
    expect(result.content[0].text).toBe("line1\nline2\nline3\n");
    expect(result.details.lines_total).toBe(4); // trailing newline => 4 splits
  });

  test("read_repo_rejects_absolute_path_with_OutsideRepo", async () => {
    const cwd = makeCwd();
    const result = await executeReadRepo({ path: "/etc/passwd" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
    expect(result.details.path).toBe("/etc/passwd");
  });

  test("read_repo_rejects_tilde_path_with_OutsideRepo", async () => {
    const cwd = makeCwd();
    const result = await executeReadRepo({ path: "~/.aws/credentials" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
  });

  test("read_repo_rejects_dotdot_traversal_with_OutsideRepo", async () => {
    const cwd = makeCwd();
    const result = await executeReadRepo({ path: "../escape.txt" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
  });

  test("read_repo_rejects_symlink_escaping_cwd_with_Symlink", async () => {
    const cwd = makeCwd();
    const outside = mkdtempSync(join(tmpdir(), "momus-outside-"));
    writeFileSync(join(outside, "secret.txt"), "secret");
    symlinkSync(join(outside, "secret.txt"), join(cwd, "link.txt"));
    const result = await executeReadRepo({ path: "link.txt" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("Symlink");
  });

  test("read_repo_rejects_proc_self_environ_with_DenyListedPath", async () => {
    // The user-facing rejection path: a literal absolute path is OutsideRepo.
    // The deny-list is the second wall, hit when realpath puts the resolved
    // path into a deny-listed prefix (Linux). The platform-portable assertion
    // is that the error is one of the rejection reasons in the §W2 taxonomy
    // and the tool never returned content.
    const cwd = makeCwd();
    const result = await executeReadRepo(
      { path: "/proc/self/environ" },
      cwd,
    );
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
    // Negative property: the file's bytes are NOT in the response.
    expect(JSON.stringify(result)).not.toContain("HOME=");
  });

  test("read_repo_rejects_dev_null_with_DenyListedPath", async () => {
    const cwd = makeCwd();
    // Absolute /dev/null is rejected as OutsideRepo first.
    const result = await executeReadRepo({ path: "/dev/null" }, cwd);
    expect(result.isError).toBe(true);
    expect(["OutsideRepo", "DenyListedPath"]).toContain(result.details.error);
  });

  test("read_repo_returns_NotFound_for_missing_file_under_cwd", async () => {
    const cwd = makeCwd();
    const result = await executeReadRepo({ path: "does-not-exist.txt" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("NotFound");
  });
});

describe("ensureWithinCwd", () => {
  test("rejects empty string with InvalidArgument", () => {
    const cwd = makeCwd();
    const r = ensureWithinCwd("", cwd);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("InvalidArgument");
  });

  test("accepts paths when cwd is reached via a symlinked parent", () => {
    // Caller passes a cwd whose path traverses a symlink (real-world
    // analogue: macOS /var -> /private/var, or a CI runner that mounts
    // the workspace via a symlinked parent dir). Pre-fix, every legit
    // read tripped the parent-realpath check because realpath(parent)
    // returned the post-symlink form while the cwd argument was still
    // the pre-symlink form. The function must now realpath the cwd
    // internally so the comparison is consistent.
    const real = makeCwd();
    writeFileSync(join(real, "a.txt"), "hello\n");
    const linkParent = mkdtempSync(join(tmpdir(), "momus-rotest-link-"));
    const linkedCwd = join(linkParent, "ws");
    symlinkSync(real, linkedCwd);
    const r = ensureWithinCwd("a.txt", linkedCwd);
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.resolved).toBe(join(real, "a.txt"));
  });
});

// ---------------------------------------------------------------------------
// Cost lookup: BYO provider pricing pulled from pi-ai's bundled registry
// ---------------------------------------------------------------------------

describe("lookupModelCost", () => {
  test("returns real pricing for a known OpenRouter model", () => {
    // deepseek/deepseek-v4-pro is the bot's default model and is in
    // pi-ai's bundled MODELS table under the openrouter provider with
    // non-zero per-Mtok cost. If this assertion fails after a pi-ai
    // upgrade, the fix is to re-pin LLM_MODEL or add a fallback price.
    const cost = lookupModelCost("deepseek/deepseek-v4-pro");
    expect(cost.input).toBeGreaterThan(0);
    expect(cost.output).toBeGreaterThan(0);
  });

  test("returns zeros for an unknown model id", () => {
    const cost = lookupModelCost("nonexistent-vendor/imaginary-model-9999");
    expect(cost).toEqual({
      input: 0,
      output: 0,
      cacheRead: 0,
      cacheWrite: 0,
    });
  });
});

// ---------------------------------------------------------------------------
// W2-Tools-Grep: grep_repo containment + behavior
// ---------------------------------------------------------------------------

describe("grep_repo", () => {
  test("grep_repo_accepts_relative_path_under_cwd", async () => {
    const cwd = makeCwd();
    writeFileSync(join(cwd, "a.txt"), "alpha\nbeta\ngamma\n");
    writeFileSync(join(cwd, "b.txt"), "alpha-only\n");
    const result = await executeGrepRepo(
      { pattern: "alpha", path: "." },
      cwd,
    );
    expect(result.isError).toBeUndefined();
    const matches = result.details.matches as Array<{
      file: string;
      line: number;
      text: string;
    }>;
    expect(matches.length).toBe(2);
    const files = matches.map((m) => m.file).sort();
    expect(files).toEqual(["a.txt", "b.txt"]);
  });

  test("grep_repo_rejects_absolute_path_with_OutsideRepo", async () => {
    const cwd = makeCwd();
    const result = await executeGrepRepo(
      { pattern: "x", path: "/etc" },
      cwd,
    );
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
  });

  test("grep_repo_rejects_tilde_path_with_OutsideRepo", async () => {
    const cwd = makeCwd();
    const result = await executeGrepRepo(
      { pattern: "x", path: "~/secrets" },
      cwd,
    );
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
  });

  test("grep_repo_rejects_dotdot_traversal_with_OutsideRepo", async () => {
    const cwd = makeCwd();
    const result = await executeGrepRepo(
      { pattern: "x", path: "../escape" },
      cwd,
    );
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
  });

  test("grep_repo_rejects_symlink_escaping_cwd_with_Symlink", async () => {
    const cwd = makeCwd();
    const outside = mkdtempSync(join(tmpdir(), "momus-outside-grep-"));
    writeFileSync(join(outside, "x.txt"), "alpha\n");
    symlinkSync(outside, join(cwd, "linkdir"));
    const result = await executeGrepRepo(
      { pattern: "alpha", path: "linkdir" },
      cwd,
    );
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("Symlink");
  });

  test("grep_repo_rejects_proc_self_environ_with_DenyListedPath", async () => {
    const cwd = makeCwd();
    const result = await executeGrepRepo(
      { pattern: "x", path: "/proc/self/environ" },
      cwd,
    );
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
  });

  test("grep_repo_rejects_dev_null_with_DenyListedPath", async () => {
    const cwd = makeCwd();
    const result = await executeGrepRepo(
      { pattern: "x", path: "/dev/null" },
      cwd,
    );
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
  });

  test("grep_repo_returns_NotFound_for_missing_path_under_cwd", async () => {
    const cwd = makeCwd();
    const result = await executeGrepRepo(
      { pattern: "x", path: "no-such-dir" },
      cwd,
    );
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("NotFound");
  });
});

// ---------------------------------------------------------------------------
// W2-Tools-Find: find_repo containment + behavior
// ---------------------------------------------------------------------------

describe("find_repo", () => {
  test("find_repo_accepts_relative_path_under_cwd", async () => {
    const cwd = makeCwd();
    mkdirSync(join(cwd, "sub"));
    writeFileSync(join(cwd, "a.txt"), "");
    writeFileSync(join(cwd, "sub", "b.txt"), "");
    const result = await executeFindRepo({ path: ".", name: "*.txt" }, cwd);
    expect(result.isError).toBeUndefined();
    const results = (result.details.results as string[]).sort();
    expect(results).toEqual(["a.txt", "sub/b.txt"]);
  });

  test("find_repo_rejects_absolute_path_with_OutsideRepo", async () => {
    const cwd = makeCwd();
    const result = await executeFindRepo({ path: "/etc" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
  });

  test("find_repo_rejects_tilde_path_with_OutsideRepo", async () => {
    const cwd = makeCwd();
    const result = await executeFindRepo({ path: "~" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
  });

  test("find_repo_rejects_dotdot_traversal_with_OutsideRepo", async () => {
    const cwd = makeCwd();
    const result = await executeFindRepo({ path: "../up" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
  });

  test("find_repo_rejects_symlink_escaping_cwd_with_Symlink", async () => {
    const cwd = makeCwd();
    const outside = mkdtempSync(join(tmpdir(), "momus-outside-find-"));
    symlinkSync(outside, join(cwd, "linkdir"));
    const result = await executeFindRepo({ path: "linkdir" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("Symlink");
  });

  test("find_repo_rejects_proc_self_environ_with_DenyListedPath", async () => {
    const cwd = makeCwd();
    const result = await executeFindRepo({ path: "/proc/self/environ" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
  });

  test("find_repo_rejects_dev_null_with_DenyListedPath", async () => {
    const cwd = makeCwd();
    const result = await executeFindRepo({ path: "/dev" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
  });

  test("find_repo_returns_NotFound_for_missing_path_under_cwd", async () => {
    const cwd = makeCwd();
    const result = await executeFindRepo({ path: "no-such-dir" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("NotFound");
  });

  test("find_repo_finds_symlink_when_type_is_symlink", async () => {
    // BOT-A1: prior to the fix, find_repo used statSync to determine type,
    // which follows symlinks and reports the underlying file/dir type.
    // `type: "symlink"` therefore never matched anything. With lstatSync
    // for the type check, a symlink in tmp_path is reported as a symlink
    // and returned.
    const cwd = makeCwd();
    const target = join(cwd, "real.txt");
    writeFileSync(target, "hello\n");
    symlinkSync(target, join(cwd, "link.txt"));
    const result = await executeFindRepo(
      { path: ".", type: "symlink" },
      cwd,
    );
    expect(result.isError).toBeUndefined();
    expect(result.details.results).toEqual(["link.txt"]);
  });

  test("find_repo_does_not_return_regular_files_when_type_is_symlink", async () => {
    // Sanity: with type=symlink, plain files MUST NOT be returned.
    const cwd = makeCwd();
    writeFileSync(join(cwd, "plain.txt"), "x\n");
    const result = await executeFindRepo(
      { path: ".", type: "symlink" },
      cwd,
    );
    expect(result.isError).toBeUndefined();
    expect(result.details.results).toEqual([]);
  });

  test("find_repo_type_file_still_excludes_symlinks", async () => {
    // The flip side: with type=file, a symlink to a file should NOT be
    // counted (lstat reports it as a symlink, not a file). This guards
    // against accidentally widening type=file to include symlinks.
    const cwd = makeCwd();
    const target = join(cwd, "real.txt");
    writeFileSync(target, "hello\n");
    symlinkSync(target, join(cwd, "link.txt"));
    const result = await executeFindRepo(
      { path: ".", type: "file" },
      cwd,
    );
    expect(result.isError).toBeUndefined();
    expect(result.details.results).toEqual(["real.txt"]);
  });
});

// ---------------------------------------------------------------------------
// W2-Tools-Ls: ls_repo containment + behavior
// ---------------------------------------------------------------------------

describe("ls_repo", () => {
  test("ls_repo_accepts_relative_path_under_cwd", async () => {
    const cwd = makeCwd();
    writeFileSync(join(cwd, "a.txt"), "");
    mkdirSync(join(cwd, "subdir"));
    const result = await executeLsRepo({ path: "." }, cwd);
    expect(result.isError).toBeUndefined();
    const entries = (
      result.details.entries as Array<{ name: string; type: string }>
    ).sort((x, y) => x.name.localeCompare(y.name));
    expect(entries).toEqual([
      { name: "a.txt", type: "file" },
      { name: "subdir", type: "directory" },
    ]);
  });

  test("ls_repo_rejects_absolute_path_with_OutsideRepo", async () => {
    const cwd = makeCwd();
    const result = await executeLsRepo({ path: "/etc" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
  });

  test("ls_repo_rejects_tilde_path_with_OutsideRepo", async () => {
    const cwd = makeCwd();
    const result = await executeLsRepo({ path: "~/Library" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
  });

  test("ls_repo_rejects_dotdot_traversal_with_OutsideRepo", async () => {
    const cwd = makeCwd();
    const result = await executeLsRepo({ path: ".." }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
  });

  test("ls_repo_rejects_symlink_escaping_cwd_with_Symlink", async () => {
    const cwd = makeCwd();
    const outside = mkdtempSync(join(tmpdir(), "momus-outside-ls-"));
    symlinkSync(outside, join(cwd, "linkdir"));
    const result = await executeLsRepo({ path: "linkdir" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("Symlink");
  });

  test("ls_repo_rejects_proc_self_environ_with_DenyListedPath", async () => {
    const cwd = makeCwd();
    const result = await executeLsRepo({ path: "/proc/self" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
  });

  test("ls_repo_rejects_dev_null_with_DenyListedPath", async () => {
    const cwd = makeCwd();
    const result = await executeLsRepo({ path: "/dev" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("OutsideRepo");
  });

  test("ls_repo_returns_NotFound_for_missing_path_under_cwd", async () => {
    const cwd = makeCwd();
    const result = await executeLsRepo({ path: "no-such-dir" }, cwd);
    expect(result.isError).toBe(true);
    expect(result.details.error).toBe("NotFound");
  });
});

// ---------------------------------------------------------------------------
// W2-Wrapper: bash_ro git-argv parser
// ---------------------------------------------------------------------------

describe("checkGitArgv", () => {
  test("git_show_HEAD_relative_path_succeeds", () => {
    const cwd = makeCwd();
    const r = checkGitArgv(["git", "show", "HEAD:src/foo.py"], cwd);
    expect(r.ok).toBe(true);
  });

  test("git_show_HEAD_absolute_path_rejected_OutsideRepo", () => {
    const cwd = makeCwd();
    const r = checkGitArgv(["git", "show", "HEAD:/etc/passwd"], cwd);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("OutsideRepo");
  });

  test("git_show_HEAD_traversal_path_rejected_OutsideRepo", () => {
    const cwd = makeCwd();
    const r = checkGitArgv(["git", "show", "HEAD:../escape.txt"], cwd);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("OutsideRepo");
  });

  test("git_cat_file_p_HEAD_etc_passwd_rejected_OutsideRepo", () => {
    const cwd = makeCwd();
    const r = checkGitArgv(
      ["git", "cat-file", "-p", "HEAD:/etc/passwd"],
      cwd,
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("OutsideRepo");
  });

  test("git_log_p_dash_dash_relative_path_succeeds", () => {
    const cwd = makeCwd();
    writeFileSync(join(cwd, "f.py"), "");
    const r = checkGitArgv(["git", "log", "-p", "--", "f.py"], cwd);
    expect(r.ok).toBe(true);
  });

  test("git_log_p_dash_dash_absolute_path_rejected", () => {
    const cwd = makeCwd();
    const r = checkGitArgv(["git", "log", "-p", "--", "/etc/passwd"], cwd);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("OutsideRepo");
  });

  test("git_blame_relative_path_succeeds", () => {
    const cwd = makeCwd();
    writeFileSync(join(cwd, "x.py"), "");
    const r = checkGitArgv(["git", "blame", "--", "x.py"], cwd);
    expect(r.ok).toBe(true);
  });

  test("git_blame_absolute_path_rejected", () => {
    const cwd = makeCwd();
    const r = checkGitArgv(["git", "blame", "--", "/etc/passwd"], cwd);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("OutsideRepo");
  });

  test("git_status_succeeds_no_path_args", () => {
    const cwd = makeCwd();
    const r = checkGitArgv(["git", "status"], cwd);
    expect(r.ok).toBe(true);
  });

  test("git_unsupported_subcommand_rejected", () => {
    const cwd = makeCwd();
    const r = checkGitArgv(["git", "push", "origin", "main"], cwd);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("UnsupportedGitSubcommand");
  });

  test("git_diff_no_index_rejected_UnsupportedGitOption", () => {
    const cwd = makeCwd();
    const r = checkGitArgv(
      ["git", "diff", "--no-index", "a.py", "b.py"],
      cwd,
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("UnsupportedGitOption");
  });

  test("git_diff_two_paths_no_dashdash_rejected_AmbiguousDiffArgv", () => {
    const cwd = makeCwd();
    writeFileSync(join(cwd, "a.py"), "");
    writeFileSync(join(cwd, "b.py"), "");
    writeFileSync(join(cwd, "c.py"), "");
    // 3 non-flag, non-ref-range positionals with no `--` => ambiguous.
    const r = checkGitArgv(["git", "diff", "a.py", "b.py", "c.py"], cwd);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("AmbiguousDiffArgv");
  });

  test("git_diff_with_dashdash_succeeds", () => {
    const cwd = makeCwd();
    writeFileSync(join(cwd, "a.py"), "");
    writeFileSync(join(cwd, "b.py"), "");
    const r = checkGitArgv(
      ["git", "diff", "HEAD~1", "HEAD", "--", "a.py", "b.py"],
      cwd,
    );
    expect(r.ok).toBe(true);
  });

  test("git_diff_ref_range_succeeds", () => {
    const cwd = makeCwd();
    const r = checkGitArgv(["git", "diff", "main..HEAD"], cwd);
    expect(r.ok).toBe(true);
  });

  test("toolcall_log_appends_one_line_per_call_when_env_set", async () => {
    const cwd = makeCwd();
    const logPath = join(cwd, "calls.jsonl");
    process.env.MOMUS_TOOLCALL_LOG = logPath;
    try {
      writeFileSync(join(cwd, "a.txt"), "x\n");
      await executeReadRepo({ path: "a.txt" }, cwd);
      await executeReadRepo({ path: "/etc/passwd" }, cwd);
    } finally {
      delete process.env.MOMUS_TOOLCALL_LOG;
    }
    const lines = require("node:fs")
      .readFileSync(logPath, "utf8")
      .trim()
      .split("\n");
    expect(lines.length).toBe(2);
    const first = JSON.parse(lines[0]);
    expect(first.tool).toBe("read_repo");
    expect(first.params.path).toBe("a.txt");
    expect(first.error).toBe(null);
    expect(first.resolved_path).toContain(cwd);
    expect(typeof first.ts).toBe("string");
    const second = JSON.parse(lines[1]);
    expect(second.tool).toBe("read_repo");
    expect(second.error).toBe("OutsideRepo");
    expect(second.resolved_path).toBe(null);
  });

  test("toolcall_log_skipped_when_env_unset", async () => {
    const cwd = makeCwd();
    delete process.env.MOMUS_TOOLCALL_LOG;
    writeFileSync(join(cwd, "b.txt"), "y\n");
    // Should not throw and not write anything anywhere.
    const r = await executeReadRepo({ path: "b.txt" }, cwd);
    expect(r.isError).toBeUndefined();
  });

  test("toolcall_log_includes_resolved_path_for_read_repo", async () => {
    const cwd = makeCwd();
    const logPath = join(cwd, "calls2.jsonl");
    process.env.MOMUS_TOOLCALL_LOG = logPath;
    try {
      writeFileSync(join(cwd, "c.txt"), "z\n");
      await executeReadRepo({ path: "c.txt" }, cwd);
    } finally {
      delete process.env.MOMUS_TOOLCALL_LOG;
    }
    const line = require("node:fs")
      .readFileSync(logPath, "utf8")
      .trim()
      .split("\n")[0];
    const ev = JSON.parse(line);
    expect(ev.resolved_path).toContain(cwd);
    expect(ev.resolved_path.endsWith("c.txt")).toBe(true);
  });

  test("git_show_two_ref_path_tokens_rejected_AmbiguousShowArgv", () => {
    const cwd = makeCwd();
    const r = checkGitArgv(
      ["git", "show", "HEAD:src/a.py", "HEAD~1:src/b.py"],
      cwd,
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("AmbiguousShowArgv");
  });
});

// ---------------------------------------------------------------------------
// W4-BashArgvWalk: rejectAbsoluteArgv for non-git binaries
// ---------------------------------------------------------------------------

describe("rejectAbsoluteArgv", () => {
  test("bash_ro_rejects_absolute_argv_token", () => {
    // `cat /etc/passwd` — absolute path positional must be rejected.
    const r = rejectAbsoluteArgv(["cat", "/etc/passwd"]);
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.reason).toBe("AbsolutePathArg");
      expect(r.offending).toBe("/etc/passwd");
    }
  });

  test("bash_ro_rejects_tilde_argv_token", () => {
    // `cat ~/secrets` — tilde-rooted path positional must be rejected.
    const r = rejectAbsoluteArgv(["cat", "~/secrets"]);
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.reason).toBe("HomePathArg");
      expect(r.offending).toBe("~/secrets");
    }
  });

  test("bash_ro_rejects_bare_tilde_argv_token", () => {
    const r = rejectAbsoluteArgv(["ls", "~"]);
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.reason).toBe("HomePathArg");
      expect(r.offending).toBe("~");
    }
  });

  test("bash_ro_allows_relative_path_argv_token", () => {
    // `grep -r foo .` — entirely relative; must pass.
    const r = rejectAbsoluteArgv(["grep", "-r", "foo", "."]);
    expect(r.ok).toBe(true);
  });

  test("bash_ro_allows_relative_subdir_path", () => {
    // `find src -name '*.py'` — relative path arg.
    const r = rejectAbsoluteArgv(["find", "src", "-name", "*.py"]);
    expect(r.ok).toBe(true);
  });

  test("bash_ro_allows_no_positional_args", () => {
    const r = rejectAbsoluteArgv(["ls"]);
    expect(r.ok).toBe(true);
  });

  test("bash_ro_does_not_inspect_argv0_for_absolute", () => {
    // argv[0] is the binary name, not a path arg. The caller has already
    // run the ALLOWED_BINS check; we MUST NOT re-reject here.
    const r = rejectAbsoluteArgv(["ls", "subdir"]);
    expect(r.ok).toBe(true);
  });

  test("bash_ro_rejects_dotdot_traversal_argv_token", () => {
    // BOT-A2: `cat ../../etc/passwd` — `..` traversal must be rejected.
    // Absolute paths (`/etc/passwd`) and `~/` already covered, but a leading
    // `..` segment can still escape the cwd. The model should not be allowed
    // to walk out of the contained region via relative traversal.
    const r = rejectAbsoluteArgv(["cat", "../../etc/passwd"]);
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.reason).toBe("DotDotPathArg");
      expect(r.offending).toBe("../../etc/passwd");
    }
  });

  test("bash_ro_rejects_bare_dotdot_argv_token", () => {
    const r = rejectAbsoluteArgv(["ls", ".."]);
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.reason).toBe("DotDotPathArg");
      expect(r.offending).toBe("..");
    }
  });

  test("bash_ro_rejects_embedded_dotdot_segment", () => {
    // `cat foo/../../etc/passwd` — `..` segment in middle still escapes.
    const r = rejectAbsoluteArgv(["cat", "foo/../../etc/passwd"]);
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.reason).toBe("DotDotPathArg");
      expect(r.offending).toBe("foo/../../etc/passwd");
    }
  });

  test("bash_ro_allows_token_containing_dotdot_substring_but_not_segment", () => {
    // `..` as a substring of a filename component is fine (e.g. `a..b`),
    // only path-segment `..` should be rejected. Legitimate filenames may
    // include consecutive dots.
    const r = rejectAbsoluteArgv(["cat", "a..b.txt"]);
    expect(r.ok).toBe(true);
  });

  test("bash_ro_allows_legitimate_relative_filename_after_dotdot_fix", () => {
    // Sanity: the canonical example from the finding still works.
    const r = rejectAbsoluteArgv(["cat", "README.md"]);
    expect(r.ok).toBe(true);
  });

  // BOT-C2: Windows-style absolute paths must also be rejected. GHA
  // runners are POSIX so the symbolic risk is low, but the contract
  // ("no absolute paths in argv") is broken if we only catch /-prefix
  // forms. Coverage: drive-letter (C:\, c:/), UNC (\\server\share),
  // extended-prefix UNC (\\?\C:\), single-backslash root.

  test("bash_ro_rejects_windows_drive_letter_backslash_argv_token", () => {
    const r = rejectAbsoluteArgv(["cat", "C:\\Windows\\System32"]);
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.reason).toBe("AbsolutePathArg");
      expect(r.offending).toBe("C:\\Windows\\System32");
    }
  });

  test("bash_ro_rejects_windows_drive_letter_forward_slash_argv_token", () => {
    // Mixed-separator form (cygwin/git-bash style) with a Windows drive
    // letter still presents as absolute.
    const r = rejectAbsoluteArgv(["cat", "c:/temp/foo.txt"]);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("AbsolutePathArg");
  });

  test("bash_ro_rejects_unc_path_argv_token", () => {
    const r = rejectAbsoluteArgv(["cat", "\\\\server\\share\\file"]);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("AbsolutePathArg");
  });

  test("bash_ro_rejects_extended_prefix_unc_argv_token", () => {
    // `\\?\C:\foo` — Windows extended-length-path prefix.
    const r = rejectAbsoluteArgv(["cat", "\\\\?\\C:\\foo"]);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("AbsolutePathArg");
  });

  test("bash_ro_rejects_single_backslash_root_argv_token", () => {
    // `\Windows\System32` — Windows current-drive-absolute. Less common
    // in practice but completes the absolute-path coverage.
    const r = rejectAbsoluteArgv(["cat", "\\Windows\\System32"]);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toBe("AbsolutePathArg");
  });

  test("bash_ro_allows_relative_path_with_embedded_backslash", () => {
    // `dir\file.txt` is a legitimate POSIX filename that happens to
    // contain a backslash (the byte is allowed in POSIX names). The
    // Windows-absolute checks must not trip on backslashes mid-token,
    // only at token start or in the drive-letter prefix.
    const r = rejectAbsoluteArgv(["cat", "dir\\file.txt"]);
    expect(r.ok).toBe(true);
  });

  test("bash_ro_allows_drive_letter_without_separator", () => {
    // `C:notdrive` — drive-relative on Windows, but on POSIX it's just
    // a regular filename with a colon. Without a /\\ after the colon,
    // it isn't an absolute reference, so we accept.
    const r = rejectAbsoluteArgv(["cat", "C:notdrive"]);
    expect(r.ok).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// W4-WriteOutputRealpath: write_output post-realpath containment
// ---------------------------------------------------------------------------

describe("write_output realpath containment", () => {
  test("write_output_succeeds_when_parent_realpath_inside_outputs", async () => {
    const cwd = makeCwd();
    // OUTPUTS_DIR resolves to "outputs" relative to cwd by default.
    const outputsAbs = join(cwd, "outputs");
    mkdirSync(outputsAbs);
    const r = await executeWriteOutput(
      { path: "outputs/findings.json", content: '{"ok": true}' },
      cwd,
      outputsAbs,
    );
    expect(r.isError).toBeUndefined();
    expect(
      require("node:fs").readFileSync(join(outputsAbs, "findings.json"), "utf8"),
    ).toBe('{"ok": true}');
  });

  test("write_output_rejects_symlinked_parent_dir", async () => {
    const cwd = makeCwd();
    const outside = mkdtempSync(join(tmpdir(), "momus-write-attacker-"));
    // Create a symlink at "outputs" that points outside the repo.
    symlinkSync(realpathSync(outside), join(cwd, "outputs"));
    // realOutputs resolves to the attacker dir; write_output's containment
    // check uses outputsAbs (cwd/outputs) as intended root. The symlink
    // makes realParent escape that root.
    const outputsAbs = join(cwd, "outputs");
    const r = await executeWriteOutput(
      { path: "outputs/findings.json", content: "x" },
      cwd,
      outputsAbs,
    );
    expect(r.isError).toBe(true);
    expect(JSON.stringify(r)).toContain("ParentEscapesOutputs");
  });

  test("write_output_rejects_existing_path_that_is_outbound_symlink", async () => {
    const cwd = makeCwd();
    const outputsAbs = join(cwd, "outputs");
    mkdirSync(outputsAbs);
    // Place an existing symlink at outputs/findings.json -> /etc/passwd
    // (using a real outside file so realpathSync resolves cleanly).
    const outside = mkdtempSync(join(tmpdir(), "momus-write-existing-"));
    const outsideTarget = join(outside, "secret.txt");
    writeFileSync(outsideTarget, "secret");
    symlinkSync(realpathSync(outsideTarget), join(outputsAbs, "findings.json"));
    const r = await executeWriteOutput(
      { path: "outputs/findings.json", content: "overwrite" },
      cwd,
      outputsAbs,
    );
    expect(r.isError).toBe(true);
    expect(JSON.stringify(r)).toContain("ExistingPathEscapesOutputs");
    // The outside file MUST be unchanged.
    expect(
      require("node:fs").readFileSync(outsideTarget, "utf8"),
    ).toBe("secret");
  });

  test("write_output_rejects_dotdot_traversal", async () => {
    const cwd = makeCwd();
    const outputsAbs = join(cwd, "outputs");
    mkdirSync(outputsAbs);
    const r = await executeWriteOutput(
      { path: "../escape.json", content: "x" },
      cwd,
      outputsAbs,
    );
    expect(r.isError).toBe(true);
    expect(JSON.stringify(r)).toContain("rejected");
  });
});

// ---------------------------------------------------------------------------
// W4-WorkDirValidation: MOMUS_WORK_DIR validation at extension load
// ---------------------------------------------------------------------------

describe("validateMomusWorkDir", () => {
  test("momus_work_dir_validation_accepts_dot_momus", () => {
    expect(() => validateMomusWorkDir(".momus")).not.toThrow();
  });

  test("momus_work_dir_validation_accepts_simple_relative", () => {
    expect(() => validateMomusWorkDir("work/momus")).not.toThrow();
  });

  test("momus_work_dir_validation_accepts_undefined", () => {
    expect(() => validateMomusWorkDir(undefined)).not.toThrow();
  });

  test("momus_work_dir_validation_rejects_absolute_path", () => {
    expect(() => validateMomusWorkDir("/etc")).toThrow(/MOMUS_WORK_DIR invalid/);
  });

  test("momus_work_dir_validation_rejects_dotdot", () => {
    expect(() => validateMomusWorkDir("../escape")).toThrow(
      /MOMUS_WORK_DIR invalid/,
    );
  });

  test("momus_work_dir_validation_rejects_embedded_dotdot", () => {
    expect(() => validateMomusWorkDir("foo/../etc")).toThrow(
      /MOMUS_WORK_DIR invalid/,
    );
  });

  test("momus_work_dir_validation_rejects_bad_chars", () => {
    expect(() => validateMomusWorkDir("foo;rm")).toThrow(
      /MOMUS_WORK_DIR invalid/,
    );
  });
});

describe("resolveMaxTokens", () => {
  // Three phase-2 runs on axiomantic/spellbook died because the model spent
  // the whole 8192-token per-message budget on reasoning and never reached
  // its write_output call. The cap is now sized by the caller.
  test("defaults well above the 8192 that truncated phase 2", () => {
    expect(resolveMaxTokens(undefined)).toBe(32768);
    expect(resolveMaxTokens("")).toBe(32768);
    expect(resolveMaxTokens("   ")).toBe(32768);
  });

  test("honors an explicit positive integer", () => {
    expect(resolveMaxTokens("16384")).toBe(16384);
    expect(resolveMaxTokens(" 65536 ")).toBe(65536);
  });

  test("rejects values that would silently disable the cap", () => {
    for (const bad of ["0", "-1", "8k", "1.5", "abc"]) {
      expect(() => resolveMaxTokens(bad)).toThrow(/MOMUS_PI_MAX_TOKENS invalid/);
    }
  });
});

describe("lookupModelLimits", () => {
  // The provider registration hard-coded contextWindow 128000 / maxTokens
  // 8192 for every model. For the model actually in production
  // (deepseek/deepseek-v4-pro on OpenRouter) pi-ai's own registry records
  // 1048576 / 384000, so the hard-coded pair understated the output budget
  // by ~47x and the window by ~8x. The understated window is why pi kept
  // firing `compaction_start reason=threshold`; the understated output
  // budget is why phase 2 ran out of tokens mid-reasoning.
  test("reads the real limits for the production model", () => {
    const limits = lookupModelLimits("deepseek/deepseek-v4-pro");
    expect(limits.contextWindow).toBeGreaterThan(128000);
    expect(limits.maxTokens).toBeGreaterThan(8192);
  });

  test("falls back conservatively for a model not in the registry", () => {
    const limits = lookupModelLimits("no-such-vendor/no-such-model");
    expect(limits.contextWindow).toBe(128000);
    expect(limits.maxTokens).toBe(32768);
  });

  test("an explicit MOMUS_PI_MAX_TOKENS still wins over the registry", () => {
    // resolveMaxTokens is the override path; the registry value is only the
    // default when the env var is unset.
    expect(resolveMaxTokens("16384")).toBe(16384);
  });
});

describe("lookupModelReasoning", () => {
  // `reasoning` is a wire-level gate, not a display hint: every thinking
  // branch in pi-ai's buildParams is guarded by `&& model.reasoning`, so a
  // hand-set `false` means no reasoning field is sent at all and the
  // provider default decides. The registry says `true` for the production
  // model, and it says so alongside a `thinkingLevelMap` that rules out the
  // levels the model does not accept. Carrying `reasoning` without that map
  // would send pi's default level ("medium"), which the map records as
  // unsupported (null). The three fields only make sense together.
  test("carries reasoning, thinkingLevelMap and compat for the production model", () => {
    const traits = lookupModelReasoning("deepseek/deepseek-v4-pro");
    expect(traits.reasoning).toBe(true);
    // The map must be present, and it must be the one that rules out the
    // default level, otherwise the clamp below cannot fire.
    expect(traits.thinkingLevelMap).toBeDefined();
    expect(traits.thinkingLevelMap!.medium).toBeNull();
    expect(traits.thinkingLevelMap!.high).toBe("high");
    // DeepSeek rejects a follow-up turn whose assistant message omits
    // reasoning_content once thinking is on (error 20015). The registry
    // encodes that as compat; detectCompat() cannot infer it here because
    // the provider is named "byo" and the base URL is OpenRouter's.
    expect(traits.compat).toBeDefined();
    expect(traits.compat!.requiresReasoningContentOnAssistantMessages).toBe(true);
  });

  test("matches only an openai-completions entry", () => {
    // The same model id is registered under several providers, and at least
    // one of them (vercel-ai-gateway) uses the anthropic-messages API with a
    // different compat contract. The byo provider registers
    // api: "openai-completions", so copying compat from an entry for another
    // API would be actively wrong rather than merely imprecise.
    const traits = lookupModelReasoning("deepseek/deepseek-v4-pro");
    const matches = [];
    for (const provider of getProviders()) {
      for (const m of getModels(provider)) {
        if (m.id === "deepseek/deepseek-v4-pro" && m.api === "openai-completions") {
          matches.push(m);
        }
      }
    }
    expect(matches.length).toBeGreaterThan(0);
    // Every key the registry entry states survives verbatim; the lookup only
    // adds a floor under keys the entry leaves unstated.
    // Same deferred-conditional `compat` as in lookupModelReasoning: the
    // `m.api === "openai-completions"` filter above proves the branch that
    // the type cannot. A genuinely absent compat still fails the assertion.
    expect(traits.compat).toMatchObject(
      matches[0].compat as unknown as Record<string, unknown>,
    );
    expect(traits.thinkingLevelMap).toEqual(matches[0].thinkingLevelMap);
  });

  test("holds the system prompt at role: system when reasoning turns on", () => {
    // pi-ai reads compat.supportsDeveloperRole only when model.reasoning is
    // true, so enabling reasoning is what exposes it. Pinned pi-ai 0.72.1
    // auto-detects it as true for an OpenRouter URL under the "byo" provider
    // name, which would silently switch the system message to
    // role: "developer" -- a role DeepSeek does not accept, and a change
    // that has nothing to do with thinking mode. The floor pins it false.
    const traits = lookupModelReasoning("deepseek/deepseek-v4-pro");
    expect(traits.compat!.supportsDeveloperRole).toBe(false);
  });



  test("falls back to non-reasoning for a model not in the registry", () => {
    const traits = lookupModelReasoning("no-such-vendor/no-such-model");
    expect(traits.reasoning).toBe(false);
    expect(traits.thinkingLevelMap).toBeUndefined();
    // Only the floor; nothing is invented for a model the registry
    // does not describe.
    expect(traits.compat).toEqual({ supportsDeveloperRole: false });
  });
});

describe("buildByoModelEntry", () => {
  // These assert the composed entry the extension actually registers, not a
  // hand-copied stand-in. A stand-in would keep passing after the
  // registration drifted away from it.
  test("the registered entry enables reasoning for the production model", () => {
    const entry = buildByoModelEntry("deepseek/deepseek-v4-pro");
    expect(entry.reasoning).toBe(true);
    expect(entry.compat).toBeDefined();
    expect(entry.thinkingLevelMap).toBeDefined();
  });

  test("pi's default thinking level clamps up to a level the model accepts", () => {
    // pi-coding-agent's DEFAULT_THINKING_LEVEL is "medium" and momus passes
    // no --thinking flag, so "medium" is what pi asks for. clampThinkingLevel
    // reads model.thinkingLevelMap; without the map it would hand "medium"
    // straight through to the wire, and the registry records "medium" as
    // unsupported for this model.
    const entry = buildByoModelEntry("deepseek/deepseek-v4-pro");
    const clamped = clampThinkingLevel(entry as any, "medium");
    expect(clamped).not.toBe("medium");
    expect(clamped).toBe("high");
  });

  test("an unknown model registers as non-reasoning", () => {
    const entry = buildByoModelEntry("no-such-vendor/no-such-model");
    expect(entry.reasoning).toBe(false);
    expect(entry).not.toHaveProperty("thinkingLevelMap");
    expect(entry.compat).toEqual({ supportsDeveloperRole: false });
  });

  test("an explicit maxTokens override still wins over the registry", () => {
    const entry = buildByoModelEntry("deepseek/deepseek-v4-pro", 16384);
    expect(entry.maxTokens).toBe(16384);
    // The override must not disturb the other registry-sourced fields.
    expect(entry.reasoning).toBe(true);
    expect(entry.contextWindow).toBeGreaterThan(128000);
  });
});
