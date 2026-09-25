# Changelog

## 0.1.0a3

Targets specification revision `0.1-draft.9`.

- `awp_sim`: standing approvals (AWP-APR-004), declared as `safety_policy.standing_approvals` with the approval feature; a submission within a grant is admitted without approval and its result names the grant's `approval_id`.
- `awp`: `AsyncClient.advance` returns once every subscribed per-tick channel holds a frame of the new tick, which on a stream connection may follow the result (AWP-TIM-003). A `session.resume` answered `AWP_SESSION_UNKNOWN` closes the session and forgets its actions (AWP-SES-008). `respond_approval` takes `standing`.
- `awp-sim demo` resumes after a lost connection, or opens a new session if the world no longer holds it.
- `awp_sim`: an integer beyond 2^53-1 closes the session with reason `protocol_error` and the connection with code 1002 (AWP-CTL-009); a malformed frame on a stream connection closes it with code 1002 and `AWP_MALFORMED` (AWP-DAT-010); after a lockstep resumption every per-tick channel restarts with a resync keyframe at the current tick (AWP-TIM-009); after a reset the fresh frames precede the result (AWP-PRM-006).
- `awp`: an integer beyond 2^53-1 ends the session with `session.close` and close code 1002; a malformed stream frame is dropped and its stream connection closed and re-established; frames with `resync` but not `keyframe` are malformed. While a lost stream connection is down, `AsyncClient.command` raises instead of sending inline (AWP-TRN-010). `ClientConnection.receive_frame` raises for a malformed frame, and `delivery()` reports each channel's frames and gaps.

## 0.1.0a2

Targets specification revision `0.1-draft.8`.

- `awp_sim`: the world checks the arm against its envelope after every step; leaving it emits `envelope_violation` and fails the executing action with reason `envelope` (AWP-ENV-003). `World.disturb` and `Server.disturb` inject an external disturbance.
- `awp_sim`: a stop during servo motion ends at the envelope's boundary instead of past it.
- `conformance/`: reports for Core World in both time models and the evidence for their manual rows, with tests in `tests/test_evidence.py`.

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
