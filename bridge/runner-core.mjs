import { randomUUID } from "node:crypto";

const MODE_HINTS = {
  investigate:
    "Mode: investigate. Investigate, gather evidence, form at least one evidence-backed hypothesis, and report findings. Do not modify production code unless explicitly asked.",
  implement:
    "Mode: implement. Implement the requested change, then verify it (build/tests) and report exactly what changed.",
  review:
    "Mode: review. Review the relevant code and report findings, risks, and recommendations. Do not modify code unless explicitly asked.",
};

function buildTaskText(task, mode) {
  const hint = mode ? MODE_HINTS[mode] : null;
  return hint ? `[Bridge mode: ${mode}]\n${hint}\n\nTask:\n${task}` : task;
}

function buildFeedbackText(feedback) {
  return `Reviewer feedback from the supervising agent (ChatGPT). Address it, verify your work, and report back:\n\n${feedback}`;
}

export class BridgeRunner {
  constructor(ctx, deps) {
    this.deps = deps;
    this.ctx = ctx;
    /** sessionId (string) -> record */
    this.registry = new Map();
    /** sessionId -> promise chain (per-session mutex) */
    this.locks = new Map();
  }

  agents() {
    return this.ctx.get("agents");
  }
  sessions() {
    return this.ctx.get("sessions");
  }
  defaultModel() {
    return this.ctx.get("agentDefaultModel");
  }

  // ---------------------------------------------------------------- start --
  /** Create a fresh DSH session/agent and run one epoch. Returns StructuredResult. */
  async startTask({ task, workspace, mode, epochTimeoutMs }) {
    if (typeof task !== "string" || task.trim() === "") {
      return errorResult(null, "task must be a non-empty string");
    }
    const selection = this.defaultModel().currentSelection();
    const sessionId = this.deps.SessionId(`session-${randomUUID()}`);
    const handle = await this.agents().create({
      sessionId,
      meta: { cwd: workspace },
      agentOptions: { provider: selection.provider, model: selection.model },
      // NOTE: setup must NOT return a value — the factory calls
      // `(await setup())?.commit?.()` and installModelSelection's return value
      // is not an AgentSetupCommit (mirrors the headless runner's braces).
      setup: (agentCtx) => {
        this.deps.installModelSelection(agentCtx, { current: selection, assembled: void 0 });
      },
    });
    const record = {
      id: String(sessionId),
      workspace,
      mode: mode ?? null,
      task,
      state: "starting",
      createdAt: new Date().toISOString(),
      handle,
      epochPromise: null,
      epochStartedAt: null,
      lastResult: null,
    };
    this.registry.set(record.id, record);
    const epoch = this.launchEpoch(record, buildTaskText(task, mode));
    record.epochPromise = epoch;
    return this.awaitEpoch(record, epoch, epochTimeoutMs);
  }

  // ------------------------------------------------------------- continue --
  /**
   * Continue an existing session: same agent, same session log, same context.
   * If the agent is still alive in this process, reuse it; otherwise resume
   * the persisted session (bridge restart). Returns StructuredResult.
   */
  async continueTask({ sessionId, feedback, epochTimeoutMs }) {
    if (typeof feedback !== "string" || feedback.trim() === "") {
      return errorResult(sessionId, "feedback must be a non-empty string");
    }
    // The record may not be in memory (e.g. the bridge restarted) — a stale
    // record is created lazily and the agent is resumed from persistence.
    let record = this.registry.get(sessionId);
    if (!record) {
      record = {
        id: sessionId,
        workspace: null,
        mode: null,
        task: null,
        state: "resuming",
        createdAt: new Date().toISOString(),
        handle: null,
        epochPromise: null,
        epochStartedAt: null,
        lastResult: null,
      };
      this.registry.set(sessionId, record);
    }
    if (["running", "starting", "resuming", "cancelling"].includes(record.state) && record.epochPromise) {
      return {
        session_id: sessionId,
        status: "running",
        message: "session busy: an epoch is still running; wait or cancel first",
      };
    }
    record.state = "resuming";
    const epoch = this.withLock(record, async () => {
      // (re)acquire the agent handle: live agent wins, otherwise resume from
      // the persisted session log (same session, same context, same workspace).
      let handle = record.handle;
      if (!handle) {
        const live = this.agents().get(this.deps.SessionId(sessionId));
        if (live) {
          handle = { agent: live, dispose: async () => {} };
        } else {
          const selection = this.defaultModel().currentSelection();
          handle = await this.agents().resume({
            resumeSessionId: this.deps.SessionId(sessionId),
            agentOptions: { provider: selection.provider, model: selection.model },
            setup: (agentCtx) => {
              this.deps.installModelSelection(agentCtx, { current: selection, assembled: void 0 });
            },
          });
        }
        record.handle = handle;
        record.workspace = handle.agent.session.header?.cwd ?? null;
        if (!record.task) record.task = "[resumed session]";
      }
      const check = this.deps.validateWorkspace(record.workspace, this.deps.allowedWorkspaces);
      if (!check.ok) throw new Error(check.error);
      return this.launchEpoch(record, buildFeedbackText(feedback));
    });
    record.epochPromise = epoch;
    // Fold resume/launch failures (e.g. truly unknown session id) into a
    // StructuredResult instead of rejecting the MCP call.
    const settled = epoch.then(
      (result) => result,
      (error) => {
        this.registry.delete(sessionId);
        const r = errorResult(sessionId, `cannot resume/continue session: ${error instanceof Error ? error.message : String(error)}`);
        r.status = "failed";
        return r;
      }
    );
    record.epochPromise = settled;
    return this.awaitEpoch(record, settled, epochTimeoutMs);
  }

  // --------------------------------------------------------------- status --
  getStatus({ sessionId }) {
    const record = this.registry.get(sessionId);
    if (!record) {
      return {
        session_id: sessionId,
        status: "not_found",
        message: "unknown session id in this bridge process (was the bridge restarted?)",
        last_result: null,
      };
    }
    const agent = this.agents().get(this.deps.SessionId(sessionId));
    return {
      session_id: sessionId,
      status: lifecycleStatus(record),
      agent_live: agent ? agent.status : "detached",
      workspace: record.workspace,
      mode: record.mode,
      task: truncate(record.task, 500),
      created_at: record.createdAt,
      last_result: record.lastResult,
    };
  }

  // ---------------------------------------------------------------- list --
  /** List every session this bridge process knows (recovery after a client-side timeout). */
  listSessions() {
    return [...this.registry.values()].map((record) => ({
      session_id: record.id,
      status: lifecycleStatus(record),
      workspace: record.workspace,
      mode: record.mode,
      task: truncate(record.task, 200),
      created_at: record.createdAt,
      last_result_status: record.lastResult?.status ?? null,
    }));
  }

  // --------------------------------------------------------------- cancel --
  cancel({ sessionId, reason }) {
    const record = this.registry.get(sessionId);
    if (!record) {
      return { session_id: sessionId, status: "not_found", message: "unknown session id" };
    }
    if (!["starting", "resuming", "running", "cancelling"].includes(record.state)) {
      return { session_id: sessionId, status: lifecycleStatus(record), message: "no running epoch to cancel" };
    }
    const agent = this.agents().get(this.deps.SessionId(sessionId));
    if (!agent) {
      record.state = "cancelled";
      return { session_id: sessionId, status: "cancelled", message: "no live agent; session marked cancelled" };
    }
    agent.cancel({ kind: "hook", reason: reason ?? "cancelled via dsh_cancel" });
    record.state = "cancelling";
    return {
      session_id: sessionId,
      status: "cancelling",
      message: "cancel requested; the running turn will end with aborted",
    };
  }

  /** Dispose every live agent handle (bridge shutdown). */
  async disposeAll() {
    const handles = [...this.registry.values()].map((r) => r.handle).filter(Boolean);
    await Promise.allSettled(handles.map((h) => h.dispose()));
    this.registry.clear();
  }

  // -------------------------------------------------------------- private --
  /**
   * Run one epoch on the record's agent: quiesce, append the user message,
   * wait for the driver to reach quiescence again, flush durability, then
   * summarize the events appended since the epoch start. Always resolves
   * (failures are folded into a failed StructuredResult) and updates the
   * record's state/lastResult when it settles.
   */
  async launchEpoch(record, messageText) {
    try {
      const agent = record.handle.agent;
      await agent.whenIdle();
      const firstSeq = agent.session.seq;
      record.state = "running";
      record.epochStartedAt = Date.now();
      agent.followup(
        this.deps.createUserMessage({
          content: [{ type: "text", text: messageText }],
          source: { kind: "user" },
        })
      );
      await agent.whenIdle();
      await this.sessions().flush(agent.session);
      const result = summarizeEpoch(agent, firstSeq, record);
      record.lastResult = result;
      record.state = result.status === "waiting_for_review" ? "waiting_for_review" : result.status;
      return result;
    } catch (error) {
      const result = errorResult(
        record.id,
        error instanceof Error ? error.message : String(error)
      );
      result.status = "failed";
      record.lastResult = result;
      record.state = "failed";
      return result;
    }
  }

  /** Wait for the epoch result, honoring the configured timeout. */
  async awaitEpoch(record, epochPromise, timeoutMs) {
    if (timeoutMs > 0) {
      let timer;
      let result;
      try {
        result = await Promise.race([
          epochPromise,
          new Promise((resolve) => { timer = setTimeout(() => resolve(null), timeoutMs); }),
        ]);
      } finally { clearTimeout(timer); }
      if (result === null) {
        return {
          session_id: record.id,
          status: "running",
          message: `epoch still running after ${timeoutMs}ms; poll dsh_get_status for the result`,
        };
      }
      return result;
    }
    return epochPromise;
  }

  /** Serialize operations on one record (prevents overlapping epochs). */
  withLock(record, fn) {
    const prev = this.locks.get(record.id) ?? Promise.resolve();
    const next = prev.then(fn, fn);
    this.locks.set(record.id, next.catch(() => {}));
    return next;
  }
}

// ------------------------------------------------------------- lifecycle --
function lifecycleStatus(record) {
  switch (record.state) {
    case "starting":
    case "cancelling":
      return "running";
    case "waiting_for_review":
      return "completed";
    case "blocked":
      return "waiting"; // agent stopped and is waiting for human input (e.g. ask_user_question)
    default:
      return record.state; // running | failed | cancelled
  }
}

// -------------------------------------------------------------- summarize --
const WRITE_TOOL_NAMES = new Set(["write", "edit", "str_replace_editor"]);
const SHELL_TOOL_NAMES = new Set(["bash", "pwsh", "terminal"]);
const TEST_PATTERN =
  /(^|\s)(pytest|python\s+-m\s+pytest|npm\s+(test|run\s+test)|npx\s+(jest|vitest|mocha)|node\s+--test|go\s+test|cargo\s+test|dotnet\s+test|mvn\s+test|make\s+test|ctest)(\s|$)/i;
const ACTION_LIMIT = 80;
const ERROR_LIMIT = 20;
const TEST_LIMIT = 30;

/**
 * Summarize one epoch into the StructuredResult contract. Fields that cannot
 * be derived honestly from the event log stay [] / null — never invented.
 */
function summarizeEpoch(agent, firstSeq, record) {
  const events = typeof agent.session.snapshotEvents === "function"
    ? agent.session.snapshotEvents(firstSeq)
    : agent.session.events;
  let lastText = "";
  let reason = null;
  const calls = []; // {id, name, args}
  const results = new Map(); // callId -> {isError, text}

  for (const ev of events) {
    if (ev.seq < firstSeq) continue;
    const data = ev.data ?? {};
    if (ev.type === "turn/end") {
      reason = data.reason ?? null;
      continue;
    }
    if (ev.type === "assistant/message") {
      const blocks = data.message?.content ?? [];
      const text = blocks
        .filter((b) => b?.type === "text" && typeof b.text === "string")
        .map((b) => b.text)
        .join("");
      if (text) lastText = text;
      for (const b of blocks) {
        if (b?.type === "tool-call") calls.push({ id: b.id, name: b.name ?? "", args: b.arguments });
      }
      continue;
    }
    if (ev.type === "tool/result") {
      const blocks = data.message?.content ?? [];
      for (const b of blocks) {
        if (b?.type === "tool-result") {
          const text = (b.content ?? [])
            .filter((x) => x?.type === "text" && typeof x.text === "string")
            .map((x) => x.text)
            .join("");
          results.set(b.toolCallId, { isError: b.isError === true, text });
        }
      }
      continue;
    }
  }

  const kind = reason?.kind ?? "unknown";
  const actions = [];
  const changedFiles = new Set();
  const tests = [];
  const errors = [];

  for (const call of calls.slice(0, ACTION_LIMIT)) {
    const outcome = results.get(call.id);
    const digest = argsDigest(call.args);
    const label = `${call.name}(${digest})`;
    actions.push(outcome?.isError ? `[error] ${label}` : label);
    if (WRITE_TOOL_NAMES.has(call.name)) {
      for (const p of extractPaths(call.args)) changedFiles.add(p);
    }
    if (SHELL_TOOL_NAMES.has(call.name)) {
      const command = stringArg(call.args, "command");
      if (command && TEST_PATTERN.test(command)) {
        tests.push({
          command: truncate(command, 300),
          result: outcome?.isError ? "failed" : outcome ? "passed" : "unknown",
        });
      }
    }
    if (outcome?.isError && errors.length < ERROR_LIMIT) {
      errors.push(`${call.name}: ${truncate(outcome.text || "(tool error, no text)", 400)}`);
    }
  }
  if (calls.length > ACTION_LIMIT) {
    actions.push(`… and ${calls.length - ACTION_LIMIT} more tool calls (see final_message for details)`);
  }
  if (kind === "error") {
    const e = reason?.error ?? {};
    errors.push(`turn ended with error: ${e.code ?? "UNKNOWN"}: ${truncate(e.message ?? String(reason), 500)}`);
  }

  const epochMs = record.epochStartedAt ? Date.now() - record.epochStartedAt : null;
  return {
    session_id: record.id,
    status: kind === "completed" ? "waiting_for_review" : kind === "aborted" ? "cancelled" : kind === "error" ? "failed" : kind,
    summary: truncate(lastText, 4000) || null,
    actions_taken: actions,
    facts: [],
    hypotheses: [],
    evidence: [],
    changed_files: [...changedFiles].slice(0, 100),
    tests: tests.slice(0, TEST_LIMIT),
    errors: errors.slice(0, ERROR_LIMIT),
    unknowns: [],
    recommended_next_step: null,
    needs_human: kind === "blocked",
    final_message: truncate(lastText, 30000) || null,
    turn_end_reason: kind,
    tool_calls: calls.length,
    epoch_elapsed_ms: epochMs,
    workspace: record.workspace,
    mode: record.mode,
  };
}

function errorResult(sessionId, message) {
  return {
    session_id: sessionId,
    status: "failed",
    summary: null,
    actions_taken: [],
    facts: [],
    hypotheses: [],
    evidence: [],
    changed_files: [],
    tests: [],
    errors: [message],
    unknowns: [],
    recommended_next_step: null,
    needs_human: false,
    final_message: null,
    turn_end_reason: "bridge_error",
    tool_calls: 0,
    epoch_elapsed_ms: null,
  };
}

// ---------------------------------------------------------------- helpers --
function truncate(s, n) {
  if (typeof s !== "string") return s;
  return s.length > n ? `${s.slice(0, n)}…[truncated]` : s;
}

function argsDigest(args) {
  if (typeof args === "string") return truncate(args, 160);
  if (args && typeof args === "object") {
    const s = JSON.stringify(args);
    return truncate(s ?? "", 160);
  }
  return String(args ?? "");
}

function stringArg(args, key) {
  const obj = parseArgs(args);
  if (obj && typeof obj[key] === "string") return obj[key];
  return null;
}

/** Tool-call arguments on the wire are a JSON string; normalize to an object. */
function parseArgs(args) {
  if (args && typeof args === "object") return args;
  if (typeof args === "string") {
    try {
      const v = JSON.parse(args);
      return v && typeof v === "object" ? v : null;
    } catch {
      return null;
    }
  }
  return null;
}

/** Extract plausible file paths from a tool-call's arguments (JSON string or object). */
function extractPaths(args) {
  const parsed = parseArgs(args);
  if (!parsed) return [];
  const out = [];
  const push = (v) => {
    if (typeof v === "string" && v.length > 0 && v.length < 512 && looksLikePath(v)) out.push(v);
  };
  for (const key of ["path", "file_path", "file", "target", "old_path", "new_path", "src", "dest"]) {
    if (key in parsed) push(parsed[key]);
  }
  if (typeof parsed.files === "object" && parsed.files !== null) {
    for (const f of Array.isArray(parsed.files) ? parsed.files : Object.values(parsed.files)) push(f);
  }
  if (typeof parsed.operations === "object" && parsed.operations !== null && !Array.isArray(parsed.operations)) {
    for (const op of Object.values(parsed.operations)) {
      if (op && typeof op === "object") {
        push(op.file_path ?? op.path ?? op.old_path ?? op.new_path);
      }
    }
  }
  return [...new Set(out)];
}

function looksLikePath(v) {
  return (
    v.includes("/") ||
    v.includes("\\") ||
    /\.(ts|tsx|js|jsx|mjs|cjs|json|md|py|rs|go|c|h|cpp|hpp|java|kt|cs|yaml|yml|toml|ini|cfg|sh|ps1|bat|html|css|scss|vue|svelte|sql|xml|lock|txt)$/i.test(v)
  );
}
