// Deterministic DSH-shaped test double. Never reads files or calls a model.
import { BridgeRunner } from '../runner-core.mjs';
import { validateWorkspace } from '../config.mjs';

export function fakeDsh(allowedWorkspaces, { delay = 15, resumeWorkspace } = {}) {
  const handles = new Map();
  let followups = 0;
  function create(sessionId, cwd) {
    let idle = Promise.resolve();
    let finish;
    const session = { seq: 0, events: [], header: { cwd } };
    const emit = (type, data) => session.events.push({ seq: session.seq++, type, data });
    const agent = {
      session, status: 'idle', whenIdle: () => idle,
      followup() {
        followups++;
        agent.status = 'running';
        idle = new Promise(resolve => {
          const timer = setTimeout(() => finish('completed'), delay);
          finish = kind => {
            clearTimeout(timer);
            emit('assistant/message', { message: { content: [{ type: 'text', text: 'SIMULATED epoch; no model or code execution.' }] } });
            emit('turn/end', { reason: { kind } });
            agent.status = 'idle'; resolve();
          };
        });
      },
      cancel() { finish?.('aborted'); },
    };
    const handle = { agent, dispose: async () => agent.cancel() };
    handles.set(sessionId, handle);
    return handle;
  }
  const services = {
    agents: {
      create: async ({ sessionId, meta }) => create(sessionId, meta.cwd),
      get: id => handles.get(id)?.agent,
      resume: async ({ resumeSessionId }) => {
        if (!resumeWorkspace) throw new Error('session not found');
        return create(resumeSessionId, resumeWorkspace);
      },
    },
    sessions: { flush: async () => {} },
    agentDefaultModel: { currentSelection: () => ({ provider: 'fake', model: 'fake' }) },
  };
  const runner = new BridgeRunner({ get: name => services[name] }, {
    SessionId: id => id, createUserMessage: x => x, installModelSelection() {},
    validateWorkspace, allowedWorkspaces,
  });
  return { runner, followups: () => followups, setDelay: value => { delay = value; } };
}
