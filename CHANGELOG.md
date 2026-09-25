# Changelog

## Unreleased

- `conformance/`: the lockstep report and the evidence for its manual rows, with tests for them in `tests/test_evidence.py`.

## 0.1.0a1

First release, targeting specification revision `0.1-draft.7`.

- The `ws` stream binding: `awp-sim serve --stream-binding ws` offers frames on a stream connection in the binary envelope, and `AsyncClient` uses it when offered, re-establishing it if only the stream drops (AWP-TRN-010..013).
- Beyond Core, each enabled with `awp-sim serve --features`: `task`, `approval` (the `park` action; approvers connect with `--approver-token`), `blend`, `transfer`, `sim` (seeding, snapshot and restore, replay bundles with `--replay-dir`, and `awp-sim replay`), and `servo` (a command channel). `ClientConnection` gains `update_task`, `transfer`, `reset`, `snapshot`, `restore`, `respond_approval`, and `command`.
- The lockstep session clock counts advances, so reset and restore, which now move the tick, never move it backward (AWP-TIM-013). Audit records hash frame payloads, not their base64 text.

- `awp`: `ClientConnection.submit` refuses action types the session was not granted (AWP-AGT-003); `AsyncClient` treats three heartbeat intervals without a message from the world as a lost connection (AWP-SAF-002).
- `awp_sim`: channels in modalities the agent did not declare are not granted (AWP-AGM-001); granted rates honor the agent's `max_obs_rate_hz` (AWP-AGM-002); a connection without a session is closed after at least 15 s (AWP-SES-012); `--max-duration-ms`. The demo agent does nothing when `move_to_pose` is not granted.

## 0.1.0.dev0 — unreleased

First implementation, targeting specification revision `0.1-draft.5`.

- `awp`: sans-IO `ClientConnection`, asyncio `AsyncClient`, binary and inline frame codec, canonical schema validation (receiver and sender forms), the lifecycle transition table, clock synchronization.
- `awp_sim`: sans-IO world engine for lockstep and streaming, simulated arm, WebSocket server with bearer authentication, audit log, trace recorder, in-process loopback, twelve scripted scenarios, and the `awp-sim` command.
