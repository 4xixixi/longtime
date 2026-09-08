// dsh-mcp-bridge config: environment parsing + workspace path validation.
import { spawnSync } from "node:child_process";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { statSync, realpathSync } from "node:fs";

const DEFAULT_PORT = 3420;

/**
 * Load bridge configuration from the environment.
 * All knobs are env vars prefixed with DSH_BRIDGE_; DSH_* vars that the DSH
 * tree itself reads (DSH_HOME, DSH_PERMISSION_MODE, DSH_TELEMETRY_DISABLED,
 * DSH_TOOLS_MODE, ...) pass through untouched.
 */
export function loadConfig(env = process.env, cwd = process.cwd()) {
  const host = env.DSH_BRIDGE_HOST ?? "127.0.0.1";
  const nonLoopback =
    env.DSH_BRIDGE_ALLOW_NON_LOOPBACK === "1" ||
    env.DSH_BRIDGE_ALLOW_NON_LOOPBACK === "true";
  const port = Number(env.DSH_BRIDGE_PORT ?? DEFAULT_PORT);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new Error(`DSH_BRIDGE_PORT must be an integer port, got ${env.DSH_BRIDGE_PORT}`);
  }
  // Supervisor calls are dispatches, not a place to wait for a full DSH epoch.
  // Return `running` quickly and let the next scheduled heartbeat retrieve the result.
  const epochTimeoutMs = Number(env.DSH_BRIDGE_EPOCH_TIMEOUT_MS ?? 45_000);
  if (!Number.isFinite(epochTimeoutMs) || epochTimeoutMs < 0) {
    throw new Error(`DSH_BRIDGE_EPOCH_TIMEOUT_MS must be >= 0 (0 = no timeout), got ${env.DSH_BRIDGE_EPOCH_TIMEOUT_MS}`);
  }
  const stdio = env.DSH_BRIDGE_STDIO === "1" || env.DSH_BRIDGE_STDIO === "true";
  if (!nonLoopback && !["127.0.0.1", "::1", "localhost"].includes(host)) {
    throw new Error("non-loopback host requires DSH_BRIDGE_ALLOW_NON_LOOPBACK=1");
  }
  const dshInstall = env.DSH_BRIDGE_INSTALL ?? discoverDshInstall(env);
  const allowedWorkspaces = (env.DSH_BRIDGE_WORKSPACES ?? cwd)
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => resolve(s));
  if (!allowedWorkspaces.length) throw new Error("DSH_BRIDGE_WORKSPACES cannot be empty");
  return { host, port, nonLoopback, epochTimeoutMs, stdio, dshInstall, allowedWorkspaces };
}

function fileExists(p) {
  try { return statSync(p).isFile(); } catch { return false; }
}

function dirExists(p) {
  try {
    return statSync(p).isDirectory();
  } catch {
    return false;
  }
}

/**
 * Locate the installed `@deepseek-ai/dsh` package root.
 * Order: DSH_BRIDGE_INSTALL env > `npm root -g` > PATH lookup of the `dsh` bin.
 * The returned path must contain `lib/bin.js` and `node_modules`.
 */
export function discoverDshInstall(env = process.env) {
  if (env.DSH_BRIDGE_INSTALL) return env.DSH_BRIDGE_INSTALL;
  const npmRoot = process.platform === "win32"
    ? spawnSync("cmd.exe", ["/d", "/c", "npm root -g"], { encoding: "utf8", windowsHide: true })
    : spawnSync("npm", ["root", "-g"], { encoding: "utf8" });
  if (npmRoot.status === 0 && npmRoot.stdout) {
    const candidate = resolve(npmRoot.stdout.trim(), "@deepseek-ai", "dsh");
    if (dirExists(candidate)) return candidate;
  }
  // npm prefixes differ across Windows installs; check the standard user prefix too.
  if (env.APPDATA) {
    const candidate = join(env.APPDATA, "npm", "node_modules", "@deepseek-ai", "dsh");
    if (fileExists(join(candidate, "lib", "bin.js"))) return candidate;
  }
  const which = spawnSync(process.platform === "win32" ? "where" : "which", ["dsh"], { encoding: "utf8" });
  if (which.status === 0 && which.stdout) {
    const first = which.stdout.split(/\r?\n/).find(Boolean);
    if (first) {
      // bin is <pkg>/lib/bin.js (or a .cmd shim of it); walk up to the package root
      const abs = resolve(first.replace(/\.cmd$/, "").replace(/\.ps1$/, ""));
      for (const candidate of [dirname(dirname(abs)), dirname(abs)]) {
        if (fileExists(join(candidate, "lib", "bin.js"))) return candidate;
      }
    }
  }
  throw new Error("cannot locate the @deepseek-ai/dsh installation; set DSH_BRIDGE_INSTALL to its package root");
}

/**
 * Validate a workspace path for dsh_start_task.
 * Security rules:
 *  - must be an absolute path (relative paths are rejected)
 *  - must exist and be a directory
 *  - must equal or be inside one of the configured allowed roots
 *    (default: the bridge's own working directory; extend with
 *    DSH_BRIDGE_WORKSPACES="root1,root2")
 *  - system-wide roots like /, C:\, ~, /etc are therefore rejected unless
 *    explicitly listed as an allowed root by the operator
 * Returns { ok: true, path } or { ok: false, error }.
 */
export function validateWorkspace(candidate, allowedRoots, cwd = process.cwd()) {
  if (typeof candidate !== "string" || candidate.trim() === "") {
    return { ok: false, error: "workspace must be a non-empty string path" };
  }
  if (!isAbsolute(candidate)) {
    return { ok: false, error: `workspace must be an absolute path, got "${candidate}"` };
  }
  let norm;
  try { norm = realpathSync(candidate); } catch {
    return { ok: false, error: `workspace does not exist: ${candidate}` };
  }
  try {
    if (!statSync(norm).isDirectory()) {
      return { ok: false, error: `workspace is not a directory: ${norm}` };
    }
  } catch {
    return { ok: false, error: `workspace does not exist: ${norm}` };
  }
  if (allowedRoots.length === 0) {
    return { ok: false, error: "no allowed workspace roots configured (DSH_BRIDGE_WORKSPACES)" };
  }
  for (const root of allowedRoots) {
    let canonicalRoot;
    try { canonicalRoot = realpathSync(root); } catch { continue; }
    const rel = relative(canonicalRoot, norm);
    if (rel === "" || (!rel.startsWith("..") && !isAbsolute(rel))) {
      return { ok: true, path: norm, root };
    }
  }
  return {
    ok: false,
    error: `workspace ${norm} is not inside any allowed workspace root [${allowedRoots.join(", ")}]`,
  };
}
