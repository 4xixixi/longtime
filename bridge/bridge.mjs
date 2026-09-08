// dsh-mcp-bridge entry point.
//
// One process = MCP server + DSH host:
//   1. locate the installed @deepseek-ai/dsh package
//   2. initialize the `dsh-mcp-bridge` profile under $DSH_HOME/profiles
//      (bundles: dsh-base + dsh-headless) and copy the runner plugin in
//   3. boot the Cordis tree in-process (dsh-app-boot `boot`, same composition
//      path the `dsh` launcher uses) with the overlay profile-patch.yml
//   4. start the MCP server (Streamable HTTP on 127.0.0.1:<port>/mcp, and/or
//      stdio) whose tools drive the bridge runner inside the tree
//
// The tree is the actual DSH runtime: agent loop, session persistence,
// sandbox, tools, persona. No DSH core code is modified.
import { copyFileSync, existsSync, writeFileSync, symlinkSync, realpathSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { loadConfig } from "./config.mjs";

const NAME = "dsh";
const PROFILE_NAME = "dsh-mcp-bridge";
const ROOT_CONFIG = `# dsh profile root — an empty entry list. The tree is composed as patches:
# each bundle in package.json's dsh.profile.bundles, then cordis.patch.yml, then any
# --patch overlays. Edit cordis.patch.yml, not this file.
[]
`;
const TELEMETRY_ROW_ID = "session-telemetry-otel";

async function main() {
  const cfg = loadConfig();
  if (process.env.DSH_BRIDGE_SESSION_ROUTING === "1") {
    const { installFromDsh } = await import("./session-routing.mjs");
    await installFromDsh(cfg.dshInstall);
  }

  // ---- resolve the DSH installation and its stable entry modules ----------
  const installAnchor = join(cfg.dshInstall, "package.json");
  if (!existsSync(installAnchor)) {
    throw new Error(`DSH installation not found at ${cfg.dshInstall} (missing package.json)`);
  }
  const req = createRequire(pathToFileURL(join(cfg.dshInstall, "lib", "bin.js")));
  const resolveMod = (spec) => import(pathToFileURL(req.resolve(spec)));
  const appBoot = await resolveMod("@deepseek-ai/dsh-app-boot");
  const cmdline = await resolveMod("@deepseek-ai/dsh-cmdline");
  const homePaths = await resolveMod("@deepseek-ai/dsh-home-paths");
  const launchEnv = await resolveMod("@deepseek-ai/dsh-launch-environment");
  const environment = appBoot.loadLayeredEnv(NAME);

  // ---- ensure the bridge profile exists ------------------------------------
  // (bundles resolve from the dsh installation; the Loader's baseUrl anchors
  // on the profile directory, so the runner plugin is referenced relatively)
  const home = homePaths.resolveDshHome();
  const profileDir = join(home, "profiles", PROFILE_NAME);
  appBoot.initProfile(profileDir, ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless"]);
  // The DSH loader resolves bare plugin imports from the profile directory.
  // A fresh DSH_HOME does not inherit a global npm installation's dependencies.
  const dependencyRoot = join(cfg.dshInstall, "node_modules");
  const profileModules = join(profileDir, "node_modules");
  if (!existsSync(profileModules)) {
    symlinkSync(dependencyRoot, profileModules, process.platform === "win32" ? "junction" : "dir");
  } else if (realpathSync(profileModules) !== realpathSync(dependencyRoot)) {
    throw new Error("Bridge profile node_modules points to another installation; use a separate DSH_HOME or reconcile it manually.");
  }
  writeFileSync(join(profileDir, "cordis.yml"), ROOT_CONFIG);
  copyFileSync(new URL("./runner-plugin.mjs", import.meta.url), join(profileDir, "bridge-runner.mjs"));
  for (const name of ["runner-core.mjs", "config.mjs"]) {
    copyFileSync(new URL(`./${name}`, import.meta.url), join(profileDir, name));
  }

  // ---- compose the patch stack (same order as the dsh launcher) ------------
  const profile = appBoot.loadProfile(NAME, PROFILE_NAME, installAnchor);
  const homePatches = appBoot.loadOptionalPatches(NAME, join(home, "cordis.patch.yml")) ?? [];
  const overlays = appBoot.loadOverlayPatches(NAME, fileURLToPath(new URL("./profile-patch.yml", import.meta.url)));
  // Overlay-relative names resolve against the overlay source, not the profile.
  // Replace the inserted entry before composition; ordinary patches cannot rename it.
  for (const patch of overlays) {
    for (const entry of patch.insert ?? []) {
      if (entry.id === "bridge-runner") entry.name = pathToFileURL(join(profileDir, "bridge-runner.mjs")).href;
    }
  }
  const bundlePatches = profile.layers.flatMap((layer) => layer.patches);
  const rows = new Map();
  for (const row of appBoot.composeEntries([bundlePatches, profile.patches, homePatches, overlays])) {
    if (typeof row.id === "string") rows.set(row.id, row);
  }
  const allPatches = [...bundlePatches, ...profile.patches, ...homePatches, ...overlays];

  if (rows.has("agent-presets")) {
    allPatches.push({
      id: "agent-presets",
      config: { ...(rows.get("agent-presets")?.config ?? {}), roots: [{ path: fileURLToPath(new URL("./config/agent-presets/", pathToFileURL(installAnchor))), trust: "system" }] },
    });
  }
  if ((process.env.DSH_TELEMETRY_DISABLED ?? "") !== "" && rows.has(TELEMETRY_ROW_ID)) {
    allPatches.push({ id: TELEMETRY_ROW_ID, disabled: true });
  }

  // ---- boot the tree --------------------------------------------------------
  let ctx = null;
  let shuttingDown = false;
  const shutdown = async (code) => {
    if (shuttingDown) return;
    shuttingDown = true;
    try {
      if (ctx) await ctx.fiber.dispose();
    } finally {
      process.exit(code);
    }
  };
  const rootConfigPath = join(profileDir, "cordis.yml");
  ctx = await appBoot.boot(NAME, rootConfigPath, structuredClone(allPatches), (hostCtx) => {
    hostCtx.provide(launchEnv.DSH_LAUNCH_ENVIRONMENT_KEY, environment);
    cmdline.provideCmdline(hostCtx, { args: [], exit: (code) => void shutdown(code) });
  });
  process.on("SIGINT", () => void shutdown(130));
  process.on("SIGTERM", () => void shutdown(0));
  appBoot.installFailLoud(NAME, process, async () => {
    if (ctx) await ctx.fiber.dispose();
  });

  await ctx.get("loader")?.await();
  const runnerUrl = pathToFileURL(join(profileDir, "bridge-runner.mjs"));
  const runnerModule = await import(runnerUrl.href);
  const runner = runnerModule.getRunnerInstance();
  if (!runner) {
    throw new Error("bridge-runner plugin did not publish a runner instance (tree boot problem?)");
  }

  // ---- MCP server ------------------------------------------------------------
  const { startMcpServer } = await import("./mcp-server.mjs");
  const { stop } = await startMcpServer({ cfg, runner, onShutdown: () => void shutdown(0) });
  process.on("exit", () => void stop());

  if (!cfg.stdio) {
    log(`dsh-mcp-bridge ready: MCP endpoint http://${cfg.host}:${cfg.port}/mcp`);
    log(`allowed workspace roots: ${cfg.allowedWorkspaces.join(", ")}`);
    log(`DSH home: ${home}`);
  }
}

function log(line) {
  process.stderr.write(`[bridge] ${line}\n`);
}

// Record the cause of any process-level failure (DSH's fail-loud guards may
// dispose and exit the process on unhandled rejections — log first).
process.on("unhandledRejection", (reason) => {
  log(`unhandledRejection: ${reason instanceof Error ? reason.stack ?? reason.message : String(reason)}`);
});
process.on("uncaughtException", (error) => {
  log(`uncaughtException: ${error instanceof Error ? error.stack ?? error.message : String(error)}`);
});

main().catch((error) => {
  process.stderr.write(`[bridge] fatal: ${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
  process.exit(1);
});
