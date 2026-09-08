import { startMcpServer } from '../mcp-server.mjs';
import { fakeDsh } from './fake-dsh.mjs';
const allowedWorkspaces = [process.cwd()];
const { runner } = fakeDsh(allowedWorkspaces);
const server = await startMcpServer({ cfg: { host: '127.0.0.1', port: 3421, allowedWorkspaces, epochTimeoutMs: 1000 }, runner });
console.error('SIMULATED DSH, no model or file execution: http://127.0.0.1:3421/mcp');
let stopping = false;
async function stop() {
  if (stopping) return;
  stopping = true;
  await runner.disposeAll(); await server.stop();
}
process.on('SIGINT', stop); process.on('SIGTERM', stop);
