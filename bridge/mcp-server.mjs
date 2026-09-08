// dsh-mcp-bridge MCP server.
//
// Exposes six agent-level tools (no raw filesystem/shell tools):
//   ping             — link check: ChatGPT -> MCP -> bridge
//   dsh_start_task   — start a new DSH session/epoch  (task, workspace?, mode?)
//   dsh_continue     — continue the SAME DSH session  (session_id, feedback)
//   dsh_get_status   — running | waiting | completed | failed | cancelled | not_found
//   dsh_cancel       — abort the running epoch of a session
//
// Transports: Streamable HTTP on 127.0.0.1:<port>/mcp (ChatGPT Developer
// Mode / MCP Inspector), or stdio (DSH_BRIDGE_STDIO=1).
//
// Stateless HTTP mode: the SDK forbids reusing a stateless transport across
// requests (and one Protocol can hold only one transport), so each request
// gets a fresh McpServer + transport instance — the documented stateless
// pattern. Tools are cheap to re-register; all state lives in `runner`.
import http from "node:http";
import { z } from "zod";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { validateWorkspace } from "./config.mjs";

const MCP_PATH = "/mcp";
const SERVER_INFO = { name: "dsh-mcp-bridge", version: "0.2.0" };

export async function startMcpServer({ cfg, runner, onShutdown }) {
  if (cfg.stdio) {
    const server = createMcpServer(cfg, runner);
    const transport = new StdioServerTransport();
    await server.connect(transport);
    return { stop: () => server.close() };
  }

  const loopbackOnly = !cfg.nonLoopback && cfg.host !== "127.0.0.1" && cfg.host !== "::1" && cfg.host !== "localhost";
  if (loopbackOnly) {
    throw new Error(`refusing to bind non-loopback host "${cfg.host}" without DSH_BRIDGE_ALLOW_NON_LOOPBACK=1`);
  }
  const httpServer = http.createServer(async (req, res) => {
    try {
      const authority = req.headers.host ?? "";
      const port = req.socket.localPort;
      const allowedHosts = new Set([`127.0.0.1:${port}`, `localhost:${port}`, `[::1]:${port}`]);
      if (!cfg.nonLoopback && (!allowedHosts.has(authority) || (req.headers.origin && ![...allowedHosts].some(h => req.headers.origin === `http://${h}`)))) {
        res.writeHead(403); res.end("Forbidden host or origin"); return;
      }
      const url = new URL(req.url ?? "/", "http://localhost");
      if (url.pathname !== MCP_PATH) {
        res.writeHead(404, { "content-type": "application/json" });
        res.end(JSON.stringify({ jsonrpc: "2.0", error: { code: -32601, message: `not found: ${url.pathname}` }, id: null }));
        return;
      }
      if (req.method !== "POST") {
        res.writeHead(405, { allow: "POST" }); res.end(); return;
      }
      // Stateless: one server + one transport per request.
      const server = createMcpServer(cfg, runner);
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: undefined,
        onsessioninitialized: () => {},
      });
      res.on("close", () => { void server.close().catch(() => {}); });
      await server.connect(transport);
      const chunks = [];
      let size = 0;
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 1024 * 1024) { res.writeHead(413); res.end("Body too large"); return; }
        chunks.push(chunk);
      }
      const body = Buffer.concat(chunks);
      let parsed;
      try { parsed = body.length > 0 ? JSON.parse(body.toString("utf8")) : undefined; }
      catch { res.writeHead(400); res.end("Invalid JSON"); return; }
      await transport.handleRequest(req, res, parsed);
    } catch (error) {
      process.stderr.write(`[bridge] mcp request error: ${error?.stack ?? error}\n`);
      if (!res.headersSent) {
        res.writeHead(500, { "content-type": "application/json" });
        res.end(JSON.stringify({ jsonrpc: "2.0", error: { code: -32603, message: String(error?.message ?? error) }, id: null }));
      } else {
        res.end();
      }
    }
  });
  await new Promise((resolve, reject) => {
    httpServer.once("error", reject);
    httpServer.listen(cfg.port, cfg.host, () => resolve());
  });
  return {
    port: httpServer.address().port,
    stop: async () => {
      await new Promise((resolve) => httpServer.close(() => resolve()));
    },
  };
}

// ------------------------------------------------------------------- tools --
function createMcpServer(cfg, runner) {
  const server = new McpServer(SERVER_INFO, { capabilities: { tools: {} } });

  server.registerTool(
    "ping",
    {
      title: "ping",
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
      description: "Verify the ChatGPT -> MCP -> dsh-mcp-bridge link. Returns pong.",
      inputSchema: {},
    },
    async () => textResult({ status: "ok", message: "pong" })
  );

  server.registerTool(
    "dsh_start_task",
    {
      title: "dsh_start_task",
      description:
        "Start a new DSH (DeepSeek Harness) coding-agent epoch on a fresh session. " +
        "DSH autonomously reads code, runs commands, debugs and tests inside the workspace, " +
        "then returns ONE structured result. One call = one investigation/implementation epoch, " +
        "not one shell command. Use dsh_continue with the returned session_id to keep the same " +
        "agent context (chat history, commands run, files changed).",
      inputSchema: {
        task: z
          .string()
          .min(1)
          .max(50000)
          .describe("The task for DSH, written as an epoch-level goal, e.g. 'investigate why X; run experiments; form an evidence-backed hypothesis'"),
        workspace: z
          .string()
          .optional()
          .describe("Absolute path of the workspace directory. Must exist and be inside an allowed workspace root (default: the bridge's working directory)."),
        mode: z
          .enum(["investigate", "implement", "review"])
          .optional()
          .describe("Optional epoch mode: investigate (no production changes), implement (change + verify), review (findings only)."),
      },
    },
    async (args) => {
      const workspace = args.workspace ?? cfg.allowedWorkspaces[0];
      const check = validateWorkspace(workspace, cfg.allowedWorkspaces);
      if (!check.ok) return textResult({ session_id: null, status: "failed", errors: [check.error], error: check.error });
      const result = await runner.startTask({
        task: args.task,
        workspace: check.path,
        mode: args.mode,
        epochTimeoutMs: cfg.epochTimeoutMs,
      });
      return textResult(result);
    }
  );

  server.registerTool(
    "dsh_continue",
    {
      title: "dsh_continue",
      description:
        "Continue an existing DSH session: the feedback (your review, next-step instructions) is " +
        "delivered into the SAME agent context (same session id, same conversation history, same " +
        "workspace). DSH works another epoch and returns ONE structured result. If the previous " +
        "epoch is still running, this returns status 'running' — poll dsh_get_status.",
      inputSchema: {
        session_id: z.string().min(1).describe("The session_id returned by dsh_start_task"),
        feedback: z
          .string()
          .min(1)
          .max(50000)
          .describe("Your reviewer feedback: what the last result lacked, what to verify next, what to do and not do."),
      },
    },
    async (args) => {
      const result = await runner.continueTask({
        sessionId: args.session_id,
        feedback: args.feedback,
        epochTimeoutMs: cfg.epochTimeoutMs,
      });
      return textResult(result);
    }
  );

  server.registerTool(
    "dsh_get_status",
    {
      title: "dsh_get_status",
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
      description:
        "Get the lifecycle status of a session: running / waiting / completed / failed / cancelled / not_found. " +
        "When a finished epoch is available, last_result carries the full structured result (useful after a " +
        "client-side timeout of dsh_start_task / dsh_continue).",
      inputSchema: {
        session_id: z.string().min(1).describe("The session_id returned by dsh_start_task"),
      },
    },
    async (args) => textResult(runner.getStatus({ sessionId: args.session_id }))
  );

  server.registerTool(
    "dsh_list_sessions",
    {
      title: "dsh_list_sessions",
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
      description:
        "List all sessions this bridge process knows, with their lifecycle status. " +
        "Recovery helper: if a dsh_start_task / dsh_continue call timed out on your side, " +
        "the DSH agent keeps working — list sessions to find its session_id, then use " +
        "dsh_get_status to retrieve the finished result.",
      inputSchema: {},
    },
    async () => textResult({ sessions: runner.listSessions() })
  );

  server.registerTool(
    "dsh_cancel",
    {
      title: "dsh_cancel",
      description:
        "Abort the running epoch of a session. The turn ends with 'aborted'; the session stays usable " +
        "(dsh_continue may still be called afterwards).",
      inputSchema: {
        session_id: z.string().min(1).describe("The session_id returned by dsh_start_task"),
        reason: z.string().optional().describe("Optional cancellation reason (recorded in the session log)"),
      },
    },
    async (args) => textResult(runner.cancel({ sessionId: args.session_id, reason: args.reason }))
  );

  return server;
}

function textResult(value) {
  const text = JSON.stringify(value, null, 2);
  return {
    content: [{ type: "text", text }],
    structuredContent: value,
  };
}
