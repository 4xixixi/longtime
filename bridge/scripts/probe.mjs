// Read-only ping and tool discovery; can target a real or simulated bridge.
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
const client = new Client({ name: 'longtime-probe', version: '1' });
try {
  await client.connect(new StreamableHTTPClientTransport(new URL(process.env.MCP_URL ?? 'http://127.0.0.1:3420/mcp')));
  const result = (await client.callTool({ name: 'ping', arguments: {} })).structuredContent;
  if (result?.message !== 'pong') throw new Error('Unexpected ping response');
  console.log(JSON.stringify({ ok: true, tools: (await client.listTools()).tools.map(t => t.name) }, null, 2));
} catch (error) { console.error(error.message); process.exitCode = 1; }
finally { await client.close(); }
