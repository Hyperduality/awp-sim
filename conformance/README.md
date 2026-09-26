# Conformance

![AWP: Core World, AWP-conformant against 0.1-draft.9](https://img.shields.io/badge/AWP-Core_World%2C_conformant_0.1--draft.9-555)

| Class | Configuration | Claim | Report |
|---|---|---|---|
| Core World | `awp-sim serve --mode lockstep` | Core World (lockstep): AWP-conformant against 0.1-draft.9 (awp-conformance 0.1.0a5) | [`core-world-lockstep.json`](core-world-lockstep.json) |
| Core World | `awp-sim serve` | Core World (streaming): AWP-conformant against 0.1-draft.9 (awp-conformance 0.1.0a5) | [`core-world-streaming.json`](core-world-streaming.json) |
| Core World + sim | `awp-sim serve --mode lockstep --features task,approval,blend,transfer,sim,gripper --approver-token awp-sim-approver` | Core World + sim (lockstep): AWP-conformant against 0.1-draft.9 (awp-conformance 0.1.0a5) | [`features-lockstep.json`](features-lockstep.json) |
| Core World | `awp-sim serve --features task,approval,blend,transfer,servo,gripper --stream-binding ws --approver-token awp-sim-approver --approval-timeout-ms 5000` | Core World (streaming): AWP-conformant against 0.1-draft.9 (awp-conformance 0.1.0a5) | [`features-streaming.json`](features-streaming.json) |

The reports come from awp-conformance 0.1.0a5 run against awp-sim 0.1.0a5. None has a failure or anything untested. The evidence for their `manual` rows follows (AWP-CNF-005).

Between them, the two feature configurations cover every feature awp-sim offers. They are separate runs because `sim` is lockstep-only and `servo` is streaming-only. The streaming one shortens `approval_timeout_ms` so the suite can wait out a timeout (AWP-APR-003).

## Reproduce

```bash
pip install --pre awp-sim awp-conformance
curl -LO https://raw.githubusercontent.com/Hyperduality/awp-conformance/v0.1.0a5/fixtures/awp-sim.json
awp-sim serve --mode lockstep &          # or `awp-sim serve` for streaming
export AWP_SIM_PID=$!
awp-conformance world ws://127.0.0.1:8710 --fixture awp-sim.json --out report/
```

For the feature configurations:

- serve with the flags in the table;
- use `fixtures/awp-sim-features.json` as the fixture;
- in lockstep, also pass `--profile sim`.

The fixture's e-stop hooks signal `$AWP_SIM_PID`. Without it, the e-stop test is skipped and AWP-EVT-002 is reported untested. CI runs all four configurations on every change, with shorter timers. The tests cited below run in CI too:

```bash
uv run pytest tests/test_evidence.py tests/test_features.py
```

## Manual evidence

### AWP-DAT-002, AWP-TRN-009: latest-wins delivery (streaming)

- **Drop policy.** Each connection has a send queue (`_Outbox`, [`src/awp_sim/server.py`](../src/awp_sim/server.py)):
  - For each latest-wins channel, it holds at most one unsent frame and replaces it when a newer one is produced.
  - Control messages and reliable frames queue in order, so a stalled latest-wins channel delays them by at most its one pending frame.
  - A peer that stops reading is disconnected at 10,000 pending items rather than buffered without limit.
- **Stalled-receiver test.** In `test_a_stalled_receiver_holds_one_frame_per_latest_wins_channel`, 1,000 `proprio` frames and 10 `arm_state` frames are produced while nothing drains the queue. What remains is the newest `proprio` frame alone and all 10 `arm_state` frames, in order.

### AWP-AUD-005: replay bundles (Core World + sim)

`test_a_replay_bundle_reproduces_its_session` (`tests/test_features.py`) records a seeded lockstep session, with a cancel and a second move, as a replay bundle.

- Replaying it with the engine behind `awp-sim replay` reproduces every transition and more than 80 frame hashes.
- A bundle with one tampered frame hash does not reproduce.

`awp-sim serve --replay-dir DIR --mode lockstep --features sim` writes bundles, and `awp-sim replay BUNDLE` checks one.

### AWP-DAT-008: frame test vectors

awp-sim encodes and decodes frames with awp-python's `awp.frames`. That decoder's evidence is `test_vectors` in awp-python's [`tests/test_frames.py`](https://github.com/Hyperduality/awp-python/blob/main/tests/test_frames.py), which runs it over every vector in `schemas/test-vectors/frames.json` at `spec-v0.1-draft.9`:

- the 10 valid vectors decode to their listed fields;
- the 7 marked `expect_error` are rejected with their listed error.

### AWP-ENV-003: envelope violations during execution

After every simulation step, the world checks that the arm's position is within `aabb_m` and its speed within `max_velocity_mps`. On leaving the envelope, the world:

1. stops the arm;
2. emits `world.event: envelope_violation` to every session;
3. fails the executing action with reason `envelope` once its safe abort completes.

`World.disturb(offset_m)` (or `Server.disturb`) injects an external disturbance. The tests, all in `tests/test_evidence.py`:

- `test_a_disturbance_during_execution_fails_the_action`: in each time model, the arm is pushed out of the envelope while a move is executing. The world emits `envelope_violation`, and the move ends `failed` with reason `envelope`.
- `test_commanded_motion_never_leaves_the_envelope`: 400 seeded random submissions, replacements, queued moves, stops, and cancels in each time model, checked after every advance. No violation is reported.
- `test_servo_motion_never_leaves_the_envelope`: servo setpoints toward the walls, and stops during them.

### AWP-UNI-001: SI units and unit suffixes

The fields awp-sim defines, reviewed:

| Field | Where | Unit |
|---|---|---|
| `p_m` | `proprio` payload | m |
| `v_mps` | `proprio` payload; `servo_arm` setpoints | m/s |
| `target_m` | `arm_state` payload | m |
| `max_velocity_mps` | `move_to_pose` params | m/s |
| `width_m` | `gripper_state` payload; `gripper_move` params | m |
| `phase`, `action_id` | `arm_state` and `gripper_state` payloads | not physical |

The specification's schemas define every other field. `test_world_defined_fields_carry_si_suffixes` collects every action parameter, channel schema field, and payload field across every feature. It fails on any field that lacks a unit suffix and has not been reviewed as non-physical.

### AWP-VER-009: the draft revision is named

- The [README](../README.md) and the [awp-sim page](https://www.agentworldprotocol.com/adapters/awp-sim) name `0.1-draft.9`.
- The `spec/` submodule is pinned at the tag `spec-v0.1-draft.9`.
- Each report records `"specification": "0.1-draft.9"`, and its claim names the revision.
