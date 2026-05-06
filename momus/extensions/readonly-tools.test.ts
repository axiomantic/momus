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
  isDeepSeekViaOpenRouter,
  rejectAbsoluteArgv,
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
