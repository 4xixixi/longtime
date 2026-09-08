// DSH-specific adapter; the state machine is independently testable.
import { installModelSelection } from "@deepseek-ai/dsh-agent";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { SessionId } from "@deepseek-ai/dsh-session";
import { BridgeRunner } from "./runner-core.mjs";
import { loadConfig, validateWorkspace } from "./config.mjs";
export const name = "bridge-runner";
export const inject = ["agentDefaultModel", "agents", "sessions"];
let runnerInstance = null;
export function getRunnerInstance() { return runnerInstance; }
export function apply(ctx) {
  runnerInstance = new BridgeRunner(ctx, {
    installModelSelection, createUserMessage, SessionId, validateWorkspace,
    allowedWorkspaces: loadConfig().allowedWorkspaces,
  });
  ctx.provide("bridgeRunner", runnerInstance);
  ctx.on("dispose", () => { runnerInstance = null; });
}
