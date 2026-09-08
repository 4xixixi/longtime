// Read-only checks: does not boot DSH, load credentials, or call models.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { loadConfig, validateWorkspace } from './config.mjs';

const checks = [];
try {
  const cfg = loadConfig();
  const pkg = JSON.parse(readFileSync(join(cfg.dshInstall, 'package.json'), 'utf8'));
  checks.push({ check: 'DSH package', ok: pkg.name === '@deepseek-ai/dsh', version: pkg.version });
  checks.push({ check: 'tested DSH version', ok: pkg.version === '0.1.2-rc.1', expected: '0.1.2-rc.1' });
  const req = createRequire(pathToFileURL(join(cfg.dshInstall, 'lib/bin.js')));
  for (const name of ['dsh-app-boot', 'dsh-cmdline', 'dsh-home-paths', 'dsh-launch-environment', 'dsh-agent', 'dsh-llm', 'dsh-session']) {
    req.resolve(`@deepseek-ai/${name}`);
    checks.push({ check: name, ok: true });
  }
  for (const root of cfg.allowedWorkspaces) {
    checks.push({ check: 'workspace', ...validateWorkspace(root, cfg.allowedWorkspaces) });
  }
} catch (error) { checks.push({ check: 'configuration', ok: false, error: error.message }); }
const ok = checks.every(c => c.ok);
console.log(JSON.stringify({ ok, checks, note: 'No model/provider/credential or live boot verification performed.' }, null, 2));
process.exitCode = ok ? 0 : 1;
