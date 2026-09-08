// Intentionally fixed to the offline demo port, never the production port.
import assert from 'node:assert/strict';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
const client = new Client({ name: 'longtime-demo', version: '1' });
try {
  await client.connect(new StreamableHTTPClientTransport(new URL('http://127.0.0.1:3421/mcp')));
  const call = async (name, args = {}) => (await client.callTool({ name, arguments: args })).structuredContent;
  const first = await call('dsh_start_task', { task: 'SIMULATED investigation', mode: 'investigate' });
  const second = await call('dsh_continue', { session_id: first.session_id, feedback: 'SIMULATED review' });
  assert.equal(second.session_id, first.session_id);
  const status = await call('dsh_get_status', { session_id: first.session_id });
  assert.equal(status.status, 'completed');
  console.log(JSON.stringify({ first, second, status }, null, 2));
} finally { await client.close(); }
