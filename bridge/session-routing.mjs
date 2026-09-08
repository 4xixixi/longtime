import { createRequire } from 'node:module';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
export async function installFromDsh(install) {
  const req = createRequire(pathToFileURL(join(install, 'lib/bin.js')));
  const { PiAiAdapter } = await import(pathToFileURL(req.resolve('@deepseek-ai/dsh-llm-pi-ai')));
  installSessionRouting(PiAiAdapter);
}
const marker = Symbol.for('longtime.dsh.opencode-session-routing.v1');
export function installSessionRouting(Adapter) {
  if (Adapter.prototype[marker]) return;
  const original = Adapter.prototype.streamWithSnapshot;
  if (typeof original !== 'function') throw new Error('Unsupported DSH adapter: streamWithSnapshot unavailable');
  Adapter.prototype.streamWithSnapshot = function (options, snapshot) {
    if (options.provider !== 'opencode-go' || !options.sessionId) {
      return original.call(this, options, snapshot);
    }
    const profile = snapshot.profiles.get(options.provider);
    if (!profile) return original.call(this, options, snapshot);
    const headers = Object.fromEntries(Object.entries(profile.headers ?? {})
      .filter(([key]) => key.toLowerCase() !== 'x-opencode-session'));
    headers['x-opencode-session'] = String(options.sessionId);
    const profiles = new Map(snapshot.profiles);
    profiles.set(options.provider, { ...profile, headers });
    return original.call(this, options, { ...snapshot, profiles });
  };
  Object.defineProperty(Adapter.prototype, marker, { value: true });
}
