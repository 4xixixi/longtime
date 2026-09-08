# Changelog

## Documentation — model-tiered positioning

- Lead with lower-cost routine execution and stronger-model fallback, followed by hand-back to the original execution flow.
- Clarify model-role configuration, independent acceptance, and the distinction between state routing and automatic model selection.
- Explain the intended savings mechanisms without claiming measured savings; outline equal-quality cost comparisons that include supervision and rework.

## 0.2.0 — integrated DSH bridge

- Publish the local DSH MCP server, runtime adapter, configuration, dependency lockfile and optional session routing patch.
- Separate the DSH-independent runner core for offline protocol/lifecycle regression tests.
- Reject workspace symlink escapes and out-of-root resumed sessions; prevent concurrent feedback from enqueueing duplicate epochs.
- Bound HTTP bodies, validate loopback Host/Origin, close request transports and clear completed epoch timers.
- Resolve profile dependencies on a fresh DSH_HOME; add a read-only configuration doctor and MCP probe.
- Add offline MCP demonstrations, a PAUSED workspace initializer and transaction-backed pause/activate helper.
- Document setup, complete supervisor rounds, persistence boundaries and operations; expand Windows/Linux CI to Python and Node.

## 0.1.0 — extracted control plane

- Retain the deterministic controller, protocol documentation, templates and 124 regression tests.
- Exclude private runtime state, diagnostics, project-specific migration scripts and machine-bound integrations.
