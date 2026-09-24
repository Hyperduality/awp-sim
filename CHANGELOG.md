# Changelog

## 0.1.0.dev0 — unreleased

First implementation, targeting specification revision `0.1-draft.5`.

- `awp`: sans-IO `ClientConnection`, asyncio `AsyncClient`, binary and inline frame codec, canonical schema validation (receiver and sender forms), the lifecycle transition table, clock synchronization.
- `awp_sim`: sans-IO world engine for lockstep and streaming, simulated arm, WebSocket server with bearer authentication, audit log, trace recorder, in-process loopback, twelve scripted scenarios, and the `awp-sim` command.
