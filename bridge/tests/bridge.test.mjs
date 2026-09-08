import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, rmSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { loadConfig, validateWorkspace } from '../config.mjs';
import { startMcpServer } from '../mcp-server.mjs';
import { fakeDsh } from '../scripts/fake-dsh.mjs';
import { installSessionRouting } from '../session-routing.mjs';

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'longtime-bridge-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const allowed = join(root, 'allowed'); const outside = join(root, 'outside');
  mkdirSync(allowed); mkdirSync(outside);
  return { root, allowed, outside };
}

test('configuration validates ports, hosts, budgets and roots', () => {
  const env = { DSH_BRIDGE_INSTALL: '/fake' };
  assert.equal(loadConfig(env).epochTimeoutMs, 45000);
  for (const overrides of [{ DSH_BRIDGE_PORT: '-1' }, { DSH_BRIDGE_HOST: '0.0.0.0' }, { DSH_BRIDGE_WORKSPACES: '' }, { DSH_BRIDGE_EPOCH_TIMEOUT_MS: '-1' }]) {
    assert.throws(() => loadConfig({ ...env, ...overrides }));
  }
});

test('workspace validation rejects outside, relative and symlink escapes', t => {
  const { allowed, outside } = fixture(t);
  assert.equal(validateWorkspace(allowed, [allowed]).ok, true);
  assert.equal(validateWorkspace(outside, [allowed]).ok, false);
  assert.equal(validateWorkspace('.', [allowed]).ok, false);
  const link = join(allowed, 'escape');
  symlinkSync(outside, link, process.platform === 'win32' ? 'junction' : 'dir');
  assert.equal(validateWorkspace(link, [allowed]).ok, false);
});

test('MCP tools complete start, status, continuation and cancellation over HTTP', async t => {
  const { allowed, outside } = fixture(t);
  const { runner, setDelay } = fakeDsh([allowed], { delay: 10 });
  t.after(() => runner.disposeAll());
  const cfg = { host: '127.0.0.1', port: 0, allowedWorkspaces: [allowed], epochTimeoutMs: 1000 };
  const server = await startMcpServer({ cfg, runner });
  t.after(() => server.stop());
  const url = `http://127.0.0.1:${server.port}/mcp`;
  const client = new Client({ name: 'offline-test', version: '1' });
  t.after(() => client.close());
  await client.connect(new StreamableHTTPClientTransport(new URL(url)));
  assert.equal((await client.listTools()).tools.length, 6);
  const call = async (name, args = {}) => (await client.callTool({ name, arguments: args })).structuredContent;
  assert.equal((await call('ping')).message, 'pong');
  assert.equal((await call('dsh_start_task', { task: 'no', workspace: outside })).status, 'failed');
  const first = await call('dsh_start_task', { task: 'demo', workspace: allowed });
  assert.equal(first.status, 'waiting_for_review');
  const second = await call('dsh_continue', { session_id: first.session_id, feedback: 'review' });
  assert.equal(second.session_id, first.session_id);
  assert.equal((await call('dsh_get_status', { session_id: first.session_id })).status, 'completed');
  assert.equal((await call('dsh_list_sessions')).sessions.length, 1);
  assert.equal((await call('dsh_get_status', { session_id: 'missing' })).status, 'not_found');
  assert.equal((await call('dsh_cancel', { session_id: first.session_id })).status, 'completed');
  assert.equal((await call('dsh_get_status', { session_id: first.session_id })).status, 'completed');
  setDelay(60000); // Hold this epoch until cancellation, independent of HTTP timing.
  cfg.epochTimeoutMs = 1;
  const pending = await call('dsh_start_task', { task: 'cancel', workspace: allowed });
  assert.equal(pending.status, 'running');
  await call('dsh_cancel', { session_id: pending.session_id });
  await runner.registry.get(pending.session_id).epochPromise;
  assert.equal((await call('dsh_get_status', { session_id: pending.session_id })).status, 'cancelled');
  assert.equal((await fetch(url, { method: 'POST', headers: { Origin: 'https://example.com' }, body: '{}' })).status, 403);
  assert.equal((await fetch(url, { method: 'POST', body: '{' })).status, 400);
  assert.equal((await fetch(url, { method: 'POST', body: 'x'.repeat(1024 * 1024 + 1) })).status, 413);
  assert.equal((await fetch(url.replace('/mcp', '/other'))).status, 404);
});

test('timeout leaves epoch alive; concurrent feedback does not queue duplicate work', async t => {
  const { allowed } = fixture(t);
  const { runner, followups } = fakeDsh([allowed], { delay: 25 });
  t.after(() => runner.disposeAll());
  const first = await runner.startTask({ task: 'one', workspace: allowed, epochTimeoutMs: 1 });
  assert.equal(first.status, 'running');
  await runner.registry.get(first.session_id).epochPromise;
  const args = { sessionId: first.session_id, feedback: 'two', epochTimeoutMs: 1 };
  const [a, b] = await Promise.all([runner.continueTask(args), runner.continueTask(args)]);
  assert.equal(a.status, 'running'); assert.equal(b.status, 'running');
  await runner.registry.get(first.session_id).epochPromise;
  assert.equal(followups(), 2);
});

test('resuming a known persisted session preserves identity and validates workspace', async t => {
  const { allowed, outside } = fixture(t);
  for (const [workspace, status] of [[allowed, 'waiting_for_review'], [outside, 'failed']]) {
    const { runner, followups } = fakeDsh([allowed], { resumeWorkspace: workspace });
    t.after(() => runner.disposeAll());
    const result = await runner.continueTask({ sessionId: 'persisted', feedback: 'resume', epochTimeoutMs: 1000 });
    assert.equal(result.session_id, 'persisted'); assert.equal(result.status, status);
    assert.equal(followups(), workspace === allowed ? 1 : 0);
  }
});

test('session routing changes only the request snapshot and installs once', async () => {
  class Adapter { streamWithSnapshot(options, snapshot) { return snapshot; } }
  installSessionRouting(Adapter); const fn = Adapter.prototype.streamWithSnapshot;
  installSessionRouting(Adapter); assert.equal(Adapter.prototype.streamWithSnapshot, fn);
  const original = { profiles: new Map([['opencode-go', { headers: { 'X-Opencode-Session': 'old', Other: 'keep' } }]]) };
  const next = new Adapter().streamWithSnapshot({ provider: 'opencode-go', sessionId: 'new' }, original);
  assert.equal(next.profiles.get('opencode-go').headers['x-opencode-session'], 'new');
  assert.equal(original.profiles.get('opencode-go').headers['X-Opencode-Session'], 'old');
});
