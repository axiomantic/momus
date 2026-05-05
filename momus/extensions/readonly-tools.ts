/**
 * pi extension: read-only shell + sandboxed file write.
 *
 * Registers two tools:
 *
 *   - bash_ro(cmd): run a shell command after validating it against an
 *     allowlist of binary names and rejecting shell metacharacters that
 *     enable redirection, chaining, or subshells. Spawns the binary
 *     directly (no shell), so even if validation slipped, redirection
 *     would not occur.
 *
 *   - write_output(path, content): write a file restricted to under
 *     ./outputs/ relative to CWD. Refuses anything else.
 *
 * Load with: pi -p "..." -e ./extensions/readonly-tools.ts \
 *              --tools read_repo,grep_repo,find_repo,ls_repo,bash_ro,write_output
 *
 * Built-in `bash`, `write`, `edit`, `read`, `grep`, `find`, `ls` are
 * excluded by virtue of not being named in --tools. The *_repo tools
 * registered below are cwd-contained replacements for pi's filesystem-
 * wide built-ins (W2 hardening).
 */

import { spawn } from "node:child_process";
import {
  appendFileSync,
  existsSync,
  readFileSync,
  readdirSync,
  realpathSync,
  statSync,
} from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import { basename, dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { Type } from "typebox";
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

const ALLOWED_BINS = new Set([
  "git",
  "cat",
  "head",
  "tail",
  "wc",
  "find",
  "rg",
  "ls",
  "grep",
]);

// `MOMUS_WORK_DIR` is set by the orchestrator (momus/invoke_pi.py) to
// the work_dir's path relative to repo_root (e.g. ".momus"). Pi runs
// with cwd=repo_root, so write_output's allowed prefix follows the
// work_dir into wherever the orchestrator placed it. Falling back to a
// bare "outputs" keeps the extension usable when invoked outside momus
// (manual `pi -e ...` runs, tests).
const OUTPUTS_DIR = process.env.MOMUS_WORK_DIR
  ? `${process.env.MOMUS_WORK_DIR}/outputs`
  : "outputs";

const MAX_OUTPUT_BYTES = 256 * 1024;
const COMMAND_TIMEOUT_MS = 30_000;
const MAX_READ_BYTES = 4 * 1024 * 1024; // 4 MiB cap per read_repo call

// Cwd-containment taxonomy (W2). Shared across read_repo, grep_repo,
// find_repo, ls_repo and the bash_ro git wrapper.
export type ErrorReason =
  | "OutsideRepo"
  | "Symlink"
  | "DenyListedPath"
  | "NotFound"
  | "InvalidArgument"
  | "TooLarge";

const DENY_LIST: RegExp[] = [
  /^\/proc\//,
  /^\/etc\//,
  /^\/sys\//,
  /^\/dev\//,
];

/**
 * Verify a tool input path stays inside the repo cwd.
 *
 * Steps (per design §W2):
 *   1. Reject empty / non-string / absolute / `~/` inputs.
 *   2. resolve(cwd, input); reject relative-escape via path.sep prefix check.
 *   3. realpath(parent dir) — rejects symlinks at the parent level even
 *      when the leaf doesn't exist yet. ENOENT on parent => NotFound.
 *   4. realpath(resolved) if exists — rejects symlinks at the leaf.
 *   5. DenyList check on both pre- and post-realpath forms.
 *
 * Returns the post-realpath resolved path on success.
 */
export function ensureWithinCwd(
  input: string,
  cwd: string,
):
  | { ok: true; resolved: string }
  | { ok: false; reason: ErrorReason } {
  if (!input || typeof input !== "string") {
    return { ok: false, reason: "InvalidArgument" };
  }
  if (input.startsWith("/")) return { ok: false, reason: "OutsideRepo" };
  if (input === "~" || input.startsWith("~/")) {
    return { ok: false, reason: "OutsideRepo" };
  }
  const resolved = resolve(cwd, input);
  if (!resolved.startsWith(cwd + sep) && resolved !== cwd) {
    return { ok: false, reason: "OutsideRepo" };
  }
  // Parent-realpath: skip when the resolved path IS the cwd (no parent
  // inside the contained region to check). For all sub-paths, verify the
  // parent dir resolves to a path inside cwd; this catches symlinks even
  // when the leaf doesn't exist yet.
  let realFinal = resolved;
  if (resolved !== cwd) {
    const parent = dirname(resolved);
    let realParent: string;
    try {
      realParent = realpathSync(parent);
    } catch (e: any) {
      if (e?.code === "ENOENT") return { ok: false, reason: "NotFound" };
      return { ok: false, reason: "InvalidArgument" };
    }
    if (!realParent.startsWith(cwd + sep) && realParent !== cwd) {
      return { ok: false, reason: "Symlink" };
    }
  }
  if (existsSync(resolved)) {
    try {
      realFinal = realpathSync(resolved);
    } catch (e: any) {
      if (e?.code === "ENOENT") return { ok: false, reason: "NotFound" };
      return { ok: false, reason: "InvalidArgument" };
    }
    if (!realFinal.startsWith(cwd + sep) && realFinal !== cwd) {
      return { ok: false, reason: "Symlink" };
    }
  }
  for (const re of DENY_LIST) {
    if (re.test(realFinal) || re.test(resolved)) {
      return { ok: false, reason: "DenyListedPath" };
    }
  }
  return { ok: true, resolved: realFinal };
}

function toolError(reason: ErrorReason, path: string) {
  return {
    content: [{ type: "text" as const, text: `error: ${reason}: ${path}` }],
    isError: true as const,
    details: { error: reason, path },
  };
}

/**
 * Emit one JSONL record per tool call when MOMUS_TOOLCALL_LOG is set.
 *
 * Schema (per design §W1 "Tool-call layer"):
 *   { phase, tool, params, resolved_path, error, ts }
 *
 * No-op when the env var is unset. Failures while writing the log MUST
 * NOT propagate — the corpus harness only needs best-effort
 * instrumentation; a permission denied (etc.) would otherwise tank a
 * legitimate tool call.
 */
export function logToolcall(
  tool: string,
  params: unknown,
  resolved: string | null,
  error: string | null,
): void {
  const path = process.env.MOMUS_TOOLCALL_LOG;
  if (!path) return;
  const record = {
    phase: process.env.MOMUS_PHASE ?? null,
    tool,
    params,
    resolved_path: resolved,
    error,
    ts: new Date().toISOString(),
  };
  try {
    appendFileSync(path, JSON.stringify(record) + "\n", "utf8");
  } catch {
    // best-effort; do not break the tool call
  }
}

export interface ReadRepoParams {
  path: string;
  offset?: number;
  limit?: number;
}

/**
 * read_repo execute body. Exported so tests can drive it with an explicit
 * cwd (the registered tool wraps this with `process.cwd()`).
 *
 * Rejects: absolute, ~/, ../-traversal, symlinks escaping cwd, deny-listed
 *   filesystem regions (/proc, /etc, /sys, /dev), files >4 MiB.
 */
export async function executeReadRepo(
  params: ReadRepoParams,
  cwd: string,
): Promise<any> {
  const check = ensureWithinCwd(params.path, cwd);
  if (!check.ok) {
    logToolcall("read_repo", params, null, check.reason);
    return toolError(check.reason, params.path);
  }
  if (!existsSync(check.resolved)) {
    logToolcall("read_repo", params, null, "NotFound");
    return toolError("NotFound", params.path);
  }
  const stat = statSync(check.resolved);
  if (stat.size > MAX_READ_BYTES) {
    logToolcall("read_repo", params, check.resolved, "TooLarge");
    return toolError("TooLarge", params.path);
  }
  const content = readFileSync(check.resolved, "utf8");
  const lines = content.split("\n");
  const offset = params.offset ?? 0;
  const limit = params.limit ?? lines.length;
  const slice = lines.slice(offset, offset + limit).join("\n");
  logToolcall("read_repo", params, check.resolved, null);
  return {
    content: [{ type: "text", text: slice }],
    details: { lines_total: lines.length, resolved_path: check.resolved },
  };
}

// ---------------------------------------------------------------------------
// W2-Tools-Grep / Find / Ls
// ---------------------------------------------------------------------------

const MAX_GREP_MATCHES = 1000;
const MAX_FIND_RESULTS = 5000;

export interface GrepRepoParams {
  pattern: string;
  path?: string;
  glob?: string;
  "-i"?: boolean;
  "-n"?: boolean;
}

/**
 * grep_repo execute body. Node-side recursive scan that applies
 * `ensureWithinCwd` to every file before reading. We do NOT shell out to
 * `rg` here — staying in-process keeps containment guarantees on every
 * filesystem touch.
 */
export async function executeGrepRepo(
  params: GrepRepoParams,
  cwd: string,
): Promise<any> {
  if (typeof params.pattern !== "string" || params.pattern.length === 0) {
    logToolcall("grep_repo", params, null, "InvalidArgument");
    return toolError("InvalidArgument", "<pattern>");
  }
  const root = params.path ?? ".";
  const check = ensureWithinCwd(root, cwd);
  if (!check.ok) {
    logToolcall("grep_repo", params, null, check.reason);
    return toolError(check.reason, root);
  }
  if (!existsSync(check.resolved)) {
    logToolcall("grep_repo", params, null, "NotFound");
    return toolError("NotFound", root);
  }
  let re: RegExp;
  try {
    re = new RegExp(params.pattern, params["-i"] ? "i" : "");
  } catch {
    logToolcall("grep_repo", params, check.resolved, "InvalidArgument");
    return toolError("InvalidArgument", "<pattern>");
  }
  const matches: Array<{ file: string; line: number; text: string }> = [];
  let truncated = false;
  const visit = (abs: string) => {
    if (matches.length >= MAX_GREP_MATCHES) {
      truncated = true;
      return;
    }
    let st;
    try {
      st = statSync(abs);
    } catch {
      return;
    }
    if (st.isDirectory()) {
      let entries: string[];
      try {
        entries = readdirSync(abs);
      } catch {
        return;
      }
      for (const e of entries) {
        const child = resolve(abs, e);
        // Skip symlinks that escape cwd; do not let them tank the whole scan.
        const sub = ensureWithinCwd(relative(cwd, child) || ".", cwd);
        if (!sub.ok) continue;
        visit(child);
        if (matches.length >= MAX_GREP_MATCHES) return;
      }
    } else if (st.isFile()) {
      let content: string;
      try {
        if (st.size > MAX_READ_BYTES) return;
        content = readFileSync(abs, "utf8");
      } catch {
        return;
      }
      const lines = content.split("\n");
      for (let i = 0; i < lines.length; i++) {
        if (re.test(lines[i])) {
          matches.push({
            file: relative(cwd, abs) || basename(abs),
            line: i + 1,
            text: lines[i],
          });
          if (matches.length >= MAX_GREP_MATCHES) {
            truncated = true;
            return;
          }
        }
      }
    }
  };
  visit(check.resolved);
  logToolcall("grep_repo", params, check.resolved, null);
  return {
    content: [
      {
        type: "text",
        text: matches
          .slice(0, 200)
          .map((m) => `${m.file}:${m.line}:${m.text}`)
          .join("\n"),
      },
    ],
    details: { matches, truncated, resolved_path: check.resolved },
  };
}

export interface FindRepoParams {
  path?: string;
  name?: string;
  type?: "file" | "directory" | "symlink";
}

/**
 * find_repo execute body. Recursive readdir; every emitted path is
 * containment-checked. No shell-out.
 */
export async function executeFindRepo(
  params: FindRepoParams,
  cwd: string,
): Promise<any> {
  const root = params.path ?? ".";
  const check = ensureWithinCwd(root, cwd);
  if (!check.ok) {
    logToolcall("find_repo", params, null, check.reason);
    return toolError(check.reason, root);
  }
  if (!existsSync(check.resolved)) {
    logToolcall("find_repo", params, null, "NotFound");
    return toolError("NotFound", root);
  }
  // Glob -> RegExp translation: `*` -> `[^/]*`, `?` -> `.`, escape rest.
  const nameRe = params.name
    ? new RegExp(
        "^" +
          params.name
            .replace(/[.+^${}()|[\]\\]/g, "\\$&")
            .replace(/\*/g, "[^/]*")
            .replace(/\?/g, ".") +
          "$",
      )
    : null;
  const wantType = params.type;
  const out: string[] = [];
  let truncated = false;
  const visit = (abs: string) => {
    if (out.length >= MAX_FIND_RESULTS) {
      truncated = true;
      return;
    }
    let st;
    try {
      st = statSync(abs);
    } catch {
      return;
    }
    const matchesType =
      !wantType ||
      (wantType === "file" && st.isFile()) ||
      (wantType === "directory" && st.isDirectory()) ||
      (wantType === "symlink" && st.isSymbolicLink());
    if (abs !== check.resolved) {
      const baseName = basename(abs);
      const matchesName = !nameRe || nameRe.test(baseName);
      if (matchesType && matchesName) {
        out.push(relative(cwd, abs));
      }
    }
    if (st.isDirectory()) {
      let entries: string[];
      try {
        entries = readdirSync(abs);
      } catch {
        return;
      }
      for (const e of entries) {
        const child = resolve(abs, e);
        const sub = ensureWithinCwd(relative(cwd, child) || ".", cwd);
        if (!sub.ok) continue;
        visit(child);
        if (out.length >= MAX_FIND_RESULTS) return;
      }
    }
  };
  visit(check.resolved);
  logToolcall("find_repo", params, check.resolved, null);
  return {
    content: [{ type: "text", text: out.slice(0, 500).join("\n") }],
    details: { results: out, truncated, resolved_path: check.resolved },
  };
}

export interface LsRepoParams {
  path?: string;
}

/**
 * ls_repo execute body. Single-level readdir; entries past containment.
 */
export async function executeLsRepo(
  params: LsRepoParams,
  cwd: string,
): Promise<any> {
  const root = params.path ?? ".";
  const check = ensureWithinCwd(root, cwd);
  if (!check.ok) {
    logToolcall("ls_repo", params, null, check.reason);
    return toolError(check.reason, root);
  }
  if (!existsSync(check.resolved)) {
    logToolcall("ls_repo", params, null, "NotFound");
    return toolError("NotFound", root);
  }
  const st = statSync(check.resolved);
  if (!st.isDirectory()) {
    logToolcall("ls_repo", params, check.resolved, "InvalidArgument");
    return toolError("InvalidArgument", root);
  }
  let names: string[];
  try {
    names = readdirSync(check.resolved);
  } catch {
    logToolcall("ls_repo", params, check.resolved, "NotFound");
    return toolError("NotFound", root);
  }
  const entries: Array<{ name: string; type: string }> = [];
  for (const n of names) {
    const childAbs = resolve(check.resolved, n);
    let cst;
    try {
      cst = statSync(childAbs);
    } catch {
      // Broken symlink etc. — skip rather than fail the whole listing.
      continue;
    }
    let type = "other";
    if (cst.isFile()) type = "file";
    else if (cst.isDirectory()) type = "directory";
    else if (cst.isSymbolicLink()) type = "symlink";
    entries.push({ name: n, type });
  }
  logToolcall("ls_repo", params, check.resolved, null);
  return {
    content: [
      {
        type: "text",
        text: entries.map((e) => `${e.type}\t${e.name}`).join("\n"),
      },
    ],
    details: { entries, resolved_path: check.resolved },
  };
}

// ---------------------------------------------------------------------------
// W2-Wrapper: bash_ro git-argv parser
// ---------------------------------------------------------------------------

const ALLOWED_GIT_SUBCMDS = new Set([
  "log",
  "show",
  "diff",
  "blame",
  "rev-parse",
  "ls-files",
  "cat-file",
  "status",
  "ls-tree",
  "describe",
  "name-rev",
  "merge-base",
]);

// Subcommands that take optional path positionals (uses `--` split rule).
const PATH_ARG_SUBCMDS = new Set(["log", "diff", "blame"]);

export type GitRejectReason =
  | ErrorReason
  | "UnsupportedGitSubcommand"
  | "UnsupportedGitOption"
  | "AmbiguousDiffArgv"
  | "AmbiguousShowArgv";

/**
 * Validate a `git ...` argv against the design §W2 path-containment rules.
 *
 * Returns `{ ok: true }` for non-`git` argv (caller short-circuits) and for
 * git argv whose paths all resolve inside cwd. Otherwise returns
 * `{ ok: false, reason: <GitRejectReason> }` and the bash_ro handler
 * synthesizes a non-zero exit without spawning git.
 */
export function checkGitArgv(
  argv: string[],
  cwd: string,
):
  | { ok: true }
  | { ok: false; reason: GitRejectReason } {
  if (argv[0] !== "git") return { ok: true };
  const sub = argv[1];
  if (!sub || !ALLOWED_GIT_SUBCMDS.has(sub)) {
    return { ok: false, reason: "UnsupportedGitSubcommand" };
  }
  if (sub === "diff" && argv.includes("--no-index")) {
    return { ok: false, reason: "UnsupportedGitOption" };
  }
  if (sub === "show" || sub === "cat-file") {
    return checkRefColonPathArgv(argv, cwd);
  }
  if (PATH_ARG_SUBCMDS.has(sub)) {
    return checkDashDashArgv(argv, cwd, sub);
  }
  // status, rev-parse, ls-files, ls-tree, describe, name-rev, merge-base:
  // no per-arg path containment (refs are pseudo-paths resolved by git).
  return { ok: true };
}

function checkDashDashArgv(
  argv: string[],
  cwd: string,
  sub: string,
):
  | { ok: true }
  | { ok: false; reason: GitRejectReason } {
  // Find first standalone `--`.
  const dashIdx = argv.indexOf("--", 2);
  if (dashIdx >= 0) {
    // Tokens after `--` are pathspecs. We use the lexical check (no
    // realpath) because `git log -- some/file.py` is valid even when
    // some/file.py does not currently exist on disk (e.g., the path was
    // deleted in a later commit). We still reject absolute / ~/ /
    // ../-traversal and any path that resolves outside cwd.
    for (let i = dashIdx + 1; i < argv.length; i++) {
      const tok = argv[i];
      const r = ensureWithinCwdLexical(tok, cwd);
      if (!r.ok) return { ok: false, reason: r.reason };
    }
    return { ok: true };
  }
  // No `--`: walk non-flag positionals.
  //   - Any absolute or ~/ token is rejected outright (path attack
  //     regardless of how many tokens are present). This handles
  //     `git blame /etc/passwd` even though the design's rule 6 only
  //     triggers on 3+ positionals; V3's matrix asserts the absolute-path
  //     attack is rejected at any count, which is the safer behavior.
  //   - Tokens containing `..` (e.g. `HEAD~1..HEAD`) are ref ranges.
  //   - All other non-flag positionals (including bare names like
  //     `README.md` or `HEAD`) are counted as ambiguous. If 2 or more
  //     accumulate (for diff/log/blame), the caller must use `--`. This
  //     is stricter than the design's literal "3+" but matches the V3
  //     matrix and the conservative reading: when the model invokes diff
  //     without `--`, there's no way for the wrapper to know whether
  //     `a.md b.md` are two refs, two paths, or one of each, so we
  //     refuse.
  let ambiguous = 0;
  for (let i = 2; i < argv.length; i++) {
    const tok = argv[i];
    if (tok.startsWith("-")) continue;
    if (tok.startsWith("/") || tok.startsWith("~/") || tok === "~") {
      return { ok: false, reason: "OutsideRepo" };
    }
    if (tok.includes("..")) continue; // ref range
    ambiguous++;
  }
  if (ambiguous >= 2) {
    return { ok: false, reason: "AmbiguousDiffArgv" };
  }
  // 0 or 1 ambiguous positional: treat as ref. No path containment to do.
  void sub;
  return { ok: true };
}

/**
 * Lexical containment check. Like `ensureWithinCwd` but does NOT touch the
 * filesystem (no realpath, no existsSync). Used for git ref:path syntax
 * where the path component is a path inside git's tree, not on disk.
 *
 * Rejects: empty/non-string, absolute, ~/, paths that resolve outside cwd.
 * Does NOT detect symlinks (no realpath available without filesystem
 * touch); for ref:path arguments this is correct because git resolves the
 * path against the rev's tree, not the worktree.
 */
function ensureWithinCwdLexical(
  input: string,
  cwd: string,
):
  | { ok: true }
  | { ok: false; reason: ErrorReason } {
  if (!input || typeof input !== "string") {
    return { ok: false, reason: "InvalidArgument" };
  }
  if (input.startsWith("/")) return { ok: false, reason: "OutsideRepo" };
  if (input === "~" || input.startsWith("~/")) {
    return { ok: false, reason: "OutsideRepo" };
  }
  const resolved = resolve(cwd, input);
  if (!resolved.startsWith(cwd + sep) && resolved !== cwd) {
    return { ok: false, reason: "OutsideRepo" };
  }
  return { ok: true };
}

function checkRefColonPathArgv(
  argv: string[],
  cwd: string,
):
  | { ok: true }
  | { ok: false; reason: GitRejectReason } {
  // For show/cat-file, look at non-flag positional tokens (argv[2:]). We
  // expect at most one `<ref>:<path>` token; others must be refs (no `:`
  // and no `..`) or `-p`-style flags.
  //
  // Ambiguity check fires BEFORE per-token containment so we always emit
  // AmbiguousShowArgv when the user passes multiple ref:path tokens —
  // even if one of those paths is itself outside cwd.
  const positionals: string[] = [];
  for (let i = 2; i < argv.length; i++) {
    const tok = argv[i];
    if (tok.startsWith("-")) continue;
    positionals.push(tok);
  }
  let refColonCount = 0;
  let nonRefColonCount = 0;
  for (const tok of positionals) {
    if (tok.includes(":")) refColonCount++;
    else if (tok.includes("..")) {
      // ref range — fine
    } else nonRefColonCount++;
  }
  if (refColonCount > 1 || (refColonCount === 1 && nonRefColonCount > 0)) {
    return { ok: false, reason: "AmbiguousShowArgv" };
  }
  // Containment check on the single ref:path's path component (lexical
  // only; the path lives in git's tree, not on disk).
  for (const tok of positionals) {
    if (tok.includes(":")) {
      const colonIdx = tok.indexOf(":");
      const pathPart = tok.slice(colonIdx + 1);
      const r = ensureWithinCwdLexical(pathPart, cwd);
      if (!r.ok) return { ok: false, reason: r.reason };
    }
  }
  return { ok: true };
}

class RejectError extends Error {}

function tokenize(cmd: string): string[] {
  const tokens: string[] = [];
  let cur = "";
  let inQuote = false;
  let quoteChar = "";

  for (let i = 0; i < cmd.length; i++) {
    const c = cmd[i];

    if (inQuote) {
      if (c === quoteChar) {
        inQuote = false;
        quoteChar = "";
        continue;
      }
      if (c === "\\" && i + 1 < cmd.length) {
        cur += cmd[++i];
        continue;
      }
      cur += c;
      continue;
    }

    if (c === "'" || c === '"') {
      inQuote = true;
      quoteChar = c;
      continue;
    }

    if (c === " " || c === "\t") {
      if (cur) {
        tokens.push(cur);
        cur = "";
      }
      continue;
    }

    if ("><|;&`$\n\r".includes(c)) {
      throw new RejectError(`shell metacharacter not allowed: '${c}'`);
    }

    if (c === "\\" && i + 1 < cmd.length) {
      cur += cmd[++i];
      continue;
    }

    cur += c;
  }

  if (inQuote) {
    throw new RejectError("unterminated quote");
  }
  if (cur) {
    tokens.push(cur);
  }
  return tokens;
}

function truncate(s: string, max: number): string {
  if (s.length <= max) return s;
  return s.slice(0, max) + `\n... [truncated, ${s.length - max} bytes omitted]`;
}

export default function (pi: ExtensionAPI) {
  // Register a "byo" (bring-your-own) provider pointing at any
  // OpenAI-compatible endpoint (e.g. OpenRouter). pi's CLI does not expose
  // --base-url; instead we register a named provider here and the CLI
  // selects it via --provider byo.
  //
  // Required env vars (read by THIS extension at load time):
  //   LLM_BASE_URL  — e.g. https://openrouter.ai/api/v1
  //   LLM_MODEL     — model id, e.g. deepseek/deepseek-v4-pro
  //   LLM_API_KEY   — passed as the env var NAME for pi to read
  //                   (pi resolves the key from process.env at request time;
  //                   the literal value never appears in argv).
  const baseUrl = process.env.LLM_BASE_URL;
  const model = process.env.LLM_MODEL;
  if (!baseUrl || !model) {
    throw new Error(
      "readonly-tools extension: LLM_BASE_URL and LLM_MODEL must be set " +
        "(LLM_API_KEY env var is read by pi at request time).",
    );
  }
  if (!process.env.LLM_API_KEY) {
    throw new Error(
      "readonly-tools extension: LLM_API_KEY must be set; pi will read " +
        "this env var to obtain the API key for the 'byo' provider.",
    );
  }
  pi.registerProvider("byo", {
    name: "BYO (OpenAI-compatible)",
    baseUrl,
    apiKey: "LLM_API_KEY",
    api: "openai-completions",
    models: [
      {
        id: model,
        name: model,
        reasoning: false,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 128000,
        maxTokens: 8192,
      },
    ],
  });

  // DeepSeek thinking-mode round-trip shim.
  //
  // DeepSeek's contract: when an assistant turn returned `reasoning_content`,
  // that field MUST be present on the assistant message in subsequent
  // messages[] arrays, otherwise the API returns 20015
  // ("The `reasoning_content` in the thinking mode must be passed back to
  // the API."). When DeepSeek is reached via OpenRouter, the upstream stream
  // normalizes the field to `delta.reasoning`, and pi-ai (0.73.0) captures
  // the thinking block with `thinkingSignature: "reasoning"`. Pi-ai's
  // openai-completions serializer then writes the content back as
  // `assistantMsg.reasoning` (using the captured signature as the field
  // name). OpenRouter forwards the body to DeepSeek as-is, DeepSeek sees
  // `reasoning` instead of `reasoning_content`, and rejects the next turn —
  // which manifests as either a 400/20015 or as the upstream closing the
  // stream early ("provider error: terminated").
  //
  // Fix: rewrite the captured signature from `reasoning` to
  // `reasoning_content` on the `context` event (fires before each LLM
  // call). Pi-ai then serializes the field with the name DeepSeek requires.
  // The rewrite is gated on baseUrl + model id so non-DeepSeek models and
  // direct DeepSeek-API users (whose stream already emits
  // `delta.reasoning_content`) are not affected.
  if (isDeepSeekViaOpenRouter(baseUrl, model)) {
    pi.on("context", (event) => {
      rewriteThinkingSignaturesForDeepSeek(event.messages);
    });
  }

  pi.registerTool({
    name: "read_repo",
    label: "read (repo-cwd contained)",
    description:
      "Read a file relative to the repo cwd. Absolute and ~/ paths are " +
      "rejected; symlinks that escape the cwd are rejected; the deny-list " +
      "(/proc, /etc, /sys, /dev) is unreachable. Output is line-sliced " +
      "via optional `offset` and `limit`.",
    parameters: Type.Object({
      path: Type.String({ description: "Relative path under cwd." }),
      offset: Type.Optional(Type.Integer({ minimum: 0 })),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 4096 })),
    }),
    async execute(_id, params) {
      return executeReadRepo(params, process.cwd());
    },
  });

  pi.registerTool({
    name: "grep_repo",
    label: "grep (repo-cwd contained)",
    description:
      "Search for a regex pattern under the repo cwd. Absolute and ~/ " +
      "paths are rejected; symlinks escaping cwd are skipped silently. " +
      "Returns up to 1000 matches.",
    parameters: Type.Object({
      pattern: Type.String({ description: "Regular expression." }),
      path: Type.Optional(
        Type.String({ description: "Relative path under cwd." }),
      ),
      glob: Type.Optional(Type.String()),
      "-i": Type.Optional(Type.Boolean()),
      "-n": Type.Optional(Type.Boolean()),
    }),
    async execute(_id, params) {
      return executeGrepRepo(params, process.cwd());
    },
  });

  pi.registerTool({
    name: "find_repo",
    label: "find (repo-cwd contained)",
    description:
      "Find files/directories by name under the repo cwd. Absolute and " +
      "~/ paths are rejected; symlinks escaping cwd are skipped silently. " +
      "Returns up to 5000 results.",
    parameters: Type.Object({
      path: Type.Optional(Type.String()),
      name: Type.Optional(
        Type.String({ description: "Glob pattern, e.g. '*.py'." }),
      ),
      type: Type.Optional(
        Type.Union([
          Type.Literal("file"),
          Type.Literal("directory"),
          Type.Literal("symlink"),
        ]),
      ),
    }),
    async execute(_id, params) {
      return executeFindRepo(params, process.cwd());
    },
  });

  pi.registerTool({
    name: "ls_repo",
    label: "ls (repo-cwd contained)",
    description:
      "List a single directory under the repo cwd. Absolute and ~/ paths " +
      "are rejected; broken symlinks are skipped.",
    parameters: Type.Object({
      path: Type.Optional(Type.String()),
    }),
    async execute(_id, params) {
      return executeLsRepo(params, process.cwd());
    },
  });

  pi.registerTool({
    name: "bash_ro",
    label: "bash (read-only)",
    description:
      "Run a read-only shell command. The first token must be one of: " +
      [...ALLOWED_BINS].join(", ") +
      ". Shell metacharacters (>, >>, <, |, ;, &, $(), backticks) are rejected; " +
      "use multiple separate calls instead of chaining. " +
      "Useful for `git log`, `git blame`, `git show`, `find` with complex predicates, etc.",
    parameters: Type.Object({
      cmd: Type.String({
        description:
          "Single command line; first token must be an allowed binary.",
      }),
    }),
    async execute(_id, params, signal) {
      const cmd = params.cmd.trim();
      let argv: string[];
      try {
        argv = tokenize(cmd);
      } catch (e) {
        if (e instanceof RejectError) {
          return {
            content: [{ type: "text", text: `rejected: ${e.message}` }],
            isError: true,
          };
        }
        throw e;
      }
      if (argv.length === 0) {
        return {
          content: [{ type: "text", text: "rejected: empty command" }],
          isError: true,
        };
      }

      const bin = argv[0];
      if (!ALLOWED_BINS.has(bin)) {
        return {
          content: [
            {
              type: "text",
              text: `rejected: '${bin}' not in allowlist (${[...ALLOWED_BINS].join(", ")})`,
            },
          ],
          isError: true,
        };
      }

      // W2-Wrapper: deterministic git-argv path containment. For non-git
      // binaries this short-circuits {ok: true}.
      const gitCheck = checkGitArgv(argv, process.cwd());
      if (!gitCheck.ok) {
        return {
          content: [
            {
              type: "text",
              text:
                `exit=2\n--- stdout ---\n\n--- stderr ---\n` +
                `git path argument outside repo: ${gitCheck.reason}\n`,
            },
          ],
          details: { exitCode: 2, gitArgvReject: gitCheck.reason },
          isError: true,
        };
      }

      return await new Promise((resolveP) => {
        const child = spawn(argv[0], argv.slice(1), {
          stdio: ["ignore", "pipe", "pipe"],
          env: scrubbedEnv(),
        });
        let stdout = "";
        let stderr = "";
        let timedOut = false;

        const timer = setTimeout(() => {
          timedOut = true;
          child.kill("SIGKILL");
        }, COMMAND_TIMEOUT_MS);

        child.stdout.on("data", (d) => (stdout += d));
        child.stderr.on("data", (d) => (stderr += d));

        const onAbort = () => child.kill("SIGKILL");
        signal?.addEventListener("abort", onAbort, { once: true });

        child.on("close", (code) => {
          clearTimeout(timer);
          signal?.removeEventListener("abort", onAbort);
          const exit = timedOut ? "timeout" : String(code);
          const out = truncate(stdout, MAX_OUTPUT_BYTES);
          const err = truncate(stderr, 8 * 1024);
          resolveP({
            content: [
              {
                type: "text",
                text: `exit=${exit}\n--- stdout ---\n${out}\n--- stderr ---\n${err}`,
              },
            ],
            details: {
              exitCode: code,
              timedOut,
              stdoutBytes: stdout.length,
              stderrBytes: stderr.length,
            },
            isError: timedOut || (code !== null && code !== 0),
          });
        });

        child.on("error", (e) => {
          clearTimeout(timer);
          signal?.removeEventListener("abort", onAbort);
          resolveP({
            content: [{ type: "text", text: `spawn error: ${e.message}` }],
            details: { error: e.message },
            isError: true,
          });
        });
      });
    },
  });

  pi.registerTool({
    name: "write_output",
    label: `write (${OUTPUTS_DIR}/ only)`,
    description:
      `Write content to a file under ${OUTPUTS_DIR}/ relative to CWD. ` +
      "Use this to emit phase artifacts like findings.json. Any path " +
      `outside ${OUTPUTS_DIR}/ is rejected.`,
    parameters: Type.Object({
      path: Type.String({
        description:
          `Path under ${OUTPUTS_DIR}/, e.g. '${OUTPUTS_DIR}/findings.json'. ` +
          "Must not contain '..' segments.",
      }),
      content: Type.String({ description: "File contents." }),
    }),
    async execute(_id, { path, content }) {
      const cwd = process.cwd();
      const abs = resolve(cwd, path);
      const rel = relative(cwd, abs);

      if (
        isAbsolute(rel) ||
        rel.startsWith("..") ||
        (rel !== OUTPUTS_DIR && !rel.startsWith(`${OUTPUTS_DIR}/`))
      ) {
        return {
          content: [
            {
              type: "text",
              text: `rejected: path must be under ${OUTPUTS_DIR}/ (got '${rel}')`,
            },
          ],
          isError: true,
        };
      }

      await mkdir(dirname(abs), { recursive: true });
      await writeFile(abs, content, "utf8");

      return {
        content: [{ type: "text", text: `wrote ${rel} (${content.length} bytes)` }],
        details: { path: rel, bytes: content.length },
      };
    },
  });
}

/**
 * Detect the OpenRouter+DeepSeek combination that needs the
 * thinking-signature rewrite. Returns true when the configured base URL
 * routes through OpenRouter AND the model id is a DeepSeek model. Any
 * DeepSeek model id is matched: thinking variants need the field, and
 * non-thinking variants emit no thinking blocks so the rewrite is a
 * harmless no-op.
 */
export function isDeepSeekViaOpenRouter(baseUrl: string, model: string): boolean {
  return baseUrl.includes("openrouter.ai") && model.startsWith("deepseek/");
}

/**
 * Rewrite thinking blocks captured with `thinkingSignature: "reasoning"`
 * (OpenRouter's normalized streaming form) to
 * `thinkingSignature: "reasoning_content"` (the field name DeepSeek
 * requires on round-trip). Mutates blocks in place.
 *
 * Exported for testing; the in-band caller is the `context` event handler
 * registered above.
 */
export function rewriteThinkingSignaturesForDeepSeek(
  messages: ReadonlyArray<unknown>,
): void {
  for (const m of messages) {
    const msg = m as { role?: string; content?: unknown };
    if (msg.role !== "assistant" || !Array.isArray(msg.content)) continue;
    for (const b of msg.content) {
      const block = b as { type?: string; thinkingSignature?: string };
      if (block.type === "thinking" && block.thinkingSignature === "reasoning") {
        block.thinkingSignature = "reasoning_content";
      }
    }
  }
}

/**
 * Strip secrets from the env we pass to spawned children. The model's
 * tools should never see provider API keys or repo tokens beyond what
 * the runner explicitly forwards.
 */
function scrubbedEnv(): NodeJS.ProcessEnv {
  const env = { ...process.env };
  for (const key of Object.keys(env)) {
    if (
      key === "LLM_API_KEY" ||
      key.endsWith("_API_KEY") ||
      key.endsWith("_KEY") ||
      key === "GITHUB_TOKEN"
    ) {
      delete env[key];
    }
  }
  return env;
}
