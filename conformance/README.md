# Conformance

![AWP: Core World, AWP-conformant against 0.1-draft.9](https://img.shields.io/badge/AWP-Core_World%2C_conformant_0.1--draft.9-555)

| Class | Configuration | Claim | Report |
|---|---|---|---|
| Core World | `awp-sim serve --mode lockstep` | Core World (lockstep): AWP-conformant against 0.1-draft.9 (awp-conformance 0.1.0a3) | [`core-world-lockstep.json`](core-world-lockstep.json) |
| Core World | `awp-sim serve` | Core World (streaming): AWP-conformant against 0.1-draft.9 (awp-conformance 0.1.0a3) | [`core-world-streaming.json`](core-world-streaming.json) |
| Core World + sim | `awp-sim serve --mode lockstep --features task,approval,blend,transfer,sim --approver-token awp-sim-approver` | Core World + sim (lockstep): AWP-conformant against 0.1-draft.9 (awp-conformance 0.1.0a3) | [`features-lockstep.json`](features-lockstep.json) |
| Core World | `awp-sim serve --features task,approval,blend,transfer,servo --stream-binding ws --approver-token awp-sim-approver --approval-timeout-ms 5000` | Core World (streaming): AWP-conformant against 0.1-draft.9 (awp-conformance 0.1.0a3) | [`features-streaming.json`](features-streaming.json) |
| Core Agent | `awp-sim demo`, lockstep manifest | Core Agent (lockstep): AWP-conformant against 0.1-draft.9 (awp-conformance 0.1.0a3) | [`core-agent-lockstep.json`](core-agent-lockstep.json) |
| Core Agent | `awp-sim demo`, streaming manifest | Core Agent (streaming): AWP-conformant against 0.1-draft.9 (awp-conformance 0.1.0a3) | [`core-agent-streaming.json`](core-agent-streaming.json) |

Each report, from awp-conformance 0.1.0a3 against awp-python 0.1.0a3, has no failure and nothing untested; the evidence for its `manual` rows follows (AWP-CNF-005). The feature configurations cover every feature awp-sim offers; `sim` and `servo` need separate runs, since `sim` is lockstep-only and `servo` streaming-only. The streaming one shortens `approval_timeout_ms` so the suite can wait out a timeout (AWP-APR-003).

## Reproduce

```bash
pip install --pre awp-python awp-conformance
curl -LO https://raw.githubusercontent.com/Hyperduality/awp-conformance/v0.1.0a3/fixtures/awp-sim.json
awp-sim serve --mode lockstep &          # or `awp-sim serve` for streaming
export AWP_SIM_PID=$!
awp-conformance world ws://127.0.0.1:8710 --fixture awp-sim.json --out report/
```

For the feature configurations, serve with the flags in the table and use `fixtures/awp-sim-features.json` (and `--profile sim` in lockstep). The fixture's e-stop hooks signal `$AWP_SIM_PID`; without it the e-stop test is skipped and AWP-EVT-002 is reported untested. The agent claims test the demo agent, which is written on the `awp` client, against the suite's harness world:

```bash
awp-sim manifest --mode lockstep > manifest.json        # or --mode streaming
echo '{"proprio": {"p_m": [0, 0, 0.4], "v_mps": [0, 0, 0]}, "arm_state": {"phase": "idle", "target_m": null, "action_id": null}}' > frames.json
awp-conformance agent --manifest manifest.json --frames frames.json --out report/ -- awp-sim demo --url '{url}' --token '{token}'
```

The tests cited below run in this repository's CI:

```bash
uv run pytest tests/test_frames.py tests/test_evidence.py
```

## Manual evidence

### AWP-DAT-002, AWP-TRN-009 — latest-wins delivery (streaming)

- **Drop policy.** Each connection's send queue (`_Outbox`, [`src/awp_sim/server.py`](../src/awp_sim/server.py)) holds at most one unsent frame per latest-wins channel and replaces it when a newer one is produced; control messages and reliable frames queue in order, so a stalled latest-wins channel delays them by at most its one pending frame. A peer that stops reading is disconnected at 10,000 pending items rather than buffered without limit.
- **Stalled-receiver test.** `test_a_stalled_receiver_holds_one_frame_per_latest_wins_channel`: 1,000 `proprio` frames and 10 `arm_state` frames are produced while nothing drains the queue; one `proprio` frame, the newest, and all 10 `arm_state` frames remain, in order.

### AWP-DAT-001, AWP-DAT-009 — loss accounting at the receiver (Core Agent, lockstep)

In streaming the suite reads the agent's loss accounting from its `obs.report`; lockstep sessions send none, so the rows are `manual` there.

- `test_the_receiver_counts_seq_gaps_as_loss_but_not_the_gap_before_a_resync`: in each time model, frames skipping two `seq` values on one channel count two missing frames in `ClientConnection.delivery()`, and a resync frame after a gap of five on another counts none.
- The client holds no delta state: each frame reaches the application whole, with its `keyframe` and `resync` flags.

### AWP-AUD-005 — replay bundles (Core World + sim)

`test_a_replay_bundle_reproduces_its_session` (`tests/test_features.py`) records a seeded lockstep session with a cancel and a second move as a replay bundle, replays it with `awp-sim replay`'s engine, and reproduces every transition and more than 80 frame hashes; a bundle with one tampered frame hash does not reproduce. `awp-sim serve --replay-dir DIR --mode lockstep --features sim` writes bundles, and `awp-sim replay BUNDLE` checks one.

### AWP-DAT-008 — frame test vectors

World and agent share one decoder, `awp.frames`. `test_vectors` (`tests/test_frames.py`) decodes every vector in `schemas/test-vectors/frames.json` of the `spec/` submodule, pinned at `spec-v0.1-draft.9`: the 10 valid vectors to the listed fields, and the 7 marked `expect_error` rejected with the listed error. `test_roundtrip_without_vendor` re-encodes the 8 valid vectors without an unknown extension or reserved bits byte for byte.

### AWP-ENV-003 — envelope violations during execution

After every simulation step the world checks the arm against its envelope: position within `aabb_m`, speed within `max_velocity_mps`. On leaving it, the world stops the arm, emits `world.event: envelope_violation` to every session, and fails the executing action with reason `envelope` once its safe abort completes. An external disturbance is injected with `World.disturb(offset_m)` (or `Server.disturb`).

- `test_a_disturbance_during_execution_fails_the_action`: in each time model, a move is executing when the arm is pushed out of the envelope; the world emits `envelope_violation` and the move ends `failed` with reason `envelope`.
- `test_commanded_motion_never_leaves_the_envelope`: 400 seeded random submissions, replacements, queued moves, stops, and cancels in each time model, checked after every advance; no violation is reported.
- `test_servo_motion_never_leaves_the_envelope`: servo setpoints toward the walls, and stops during them.

### AWP-UNI-001 — SI units and unit suffixes

The fields awp-sim defines, reviewed:

| Field | Where | Unit |
|---|---|---|
| `p_m` | `proprio` payload | m |
| `v_mps` | `proprio` payload; `servo_arm` setpoints | m/s |
| `target_m` | `arm_state` payload | m |
| `max_velocity_mps` | `move_to_pose` params | m/s |
| `phase`, `action_id` | `arm_state` payload | not physical |

Every other field is defined by the specification's schemas. `test_world_defined_fields_carry_si_suffixes` collects every action parameter, channel schema field, and payload field across every feature, and fails on any field not reviewed as non-physical that lacks a unit suffix.

### AWP-VER-009 — the draft revision is named

The [README](../README.md) and the [awp-sim page](https://www.agentworldprotocol.com/adapters/awp-sim) name `0.1-draft.9`; `awp.SPEC_REVISION` is `"0.1-draft.9"`; the `spec/` submodule is pinned at the tag `spec-v0.1-draft.9`; each report records `"specification": "0.1-draft.9"` and its claim names the revision.
