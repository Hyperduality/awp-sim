# Changelog

## 0.1.0a1

First release, targeting specification revision `0.1-draft.6`.

- `awp`: `ClientConnection.submit` refuses action types the session was not granted (AWP-AGT-003); `AsyncClient` treats three heartbeat intervals without a message from the world as a lost connection (AWP-SAF-002).
- `awp_sim`: channels in modalities the agent did not declare are not granted (AWP-AGM-001); granted rates honor the agent's `max_obs_rate_hz` (AWP-AGM-002); a connection without a session is closed after at least 15 s (AWP-SES-012); `--max-duration-ms`. The demo agent does nothing when `move_to_pose` is not granted.

## 0.1.0.dev0 — unreleased

First implementation, targeting specification revision `0.1-draft.5`.

- `awp`: sans-IO `ClientConnection`, asyncio `AsyncClient`, binary and inline frame codec, canonical schema validation (receiver and sender forms), the lifecycle transition table, clock synchronization.
- `awp_sim`: sans-IO world engine for lockstep and streaming, simulated arm, WebSocket server with bearer authentication, audit log, trace recorder, in-process loopback, twelve scripted scenarios, and the `awp-sim` command.
