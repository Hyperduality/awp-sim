# Conformance

![AWP: Core World, AWP-conformant against 0.1-draft.8](https://img.shields.io/badge/AWP-Core_World%2C_conformant_0.1--draft.8-555)

| Class | Configuration | Claim | Report |
|---|---|---|---|
| Core World | `awp-sim serve --mode lockstep` | Core World (lockstep): AWP-conformant against 0.1-draft.8 (awp-conformance 0.1.0a2) | [`core-world-lockstep.json`](core-world-lockstep.json) |
| Core World | `awp-sim serve` | Core World (streaming): AWP-conformant against 0.1-draft.8 (awp-conformance 0.1.0a2) | [`core-world-streaming.json`](core-world-streaming.json) |

Each report, from awp-conformance 0.1.0a2 against awp-python 0.1.0a2, has no failure and nothing untested; the evidence for its `manual` rows follows (AWP-CNF-005). Every other configuration remains self-assessed against 0.1-draft.8.

## Reproduce

```bash
pip install --pre awp-python awp-conformance
curl -LO https://raw.githubusercontent.com/Hyperduality/awp-conformance/v0.1.0a2/fixtures/awp-sim.json
awp-sim serve --mode lockstep &          # or `awp-sim serve` for streaming
export AWP_SIM_PID=$!
awp-conformance world ws://127.0.0.1:8710 --fixture awp-sim.json --out report/
```

The fixture's e-stop hooks signal `$AWP_SIM_PID`; without it the e-stop test is skipped and AWP-EVT-002 is reported untested. The tests cited below run in this repository's CI:

```bash
uv run pytest tests/test_frames.py tests/test_evidence.py
```

## Manual evidence

### AWP-DAT-002, AWP-TRN-009 — latest-wins delivery (streaming)

- **Drop policy.** Each connection's send queue (`_Outbox`, [`src/awp_sim/server.py`](../src/awp_sim/server.py)) holds at most one unsent frame per latest-wins channel and replaces it when a newer one is produced; control messages and reliable frames queue in order, so a stalled latest-wins channel delays them by at most its one pending frame. A peer that stops reading is disconnected at 10,000 pending items rather than buffered without limit.
- **Stalled-receiver test.** `test_a_stalled_receiver_holds_one_frame_per_latest_wins_channel`: 1,000 `proprio` frames and 10 `arm_state` frames are produced while nothing drains the queue; one `proprio` frame, the newest, and all 10 `arm_state` frames remain, in order.

### AWP-DAT-008 — frame test vectors

`test_vectors` (`tests/test_frames.py`) decodes every vector in `schemas/test-vectors/frames.json` of the `spec/` submodule, pinned at `spec-v0.1-draft.8`: the 10 valid vectors to the listed fields, and the 7 marked `expect_error` rejected with the listed error. `test_roundtrip_without_vendor` re-encodes the 8 valid vectors without an unknown extension or reserved bits byte for byte.

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

The [README](../README.md) and the [awp-sim page](https://www.agentworldprotocol.com/adapters/awp-sim) name `0.1-draft.8`; `awp.SPEC_REVISION` is `"0.1-draft.8"`; the `spec/` submodule is pinned at the tag `spec-v0.1-draft.8`; each report records `"specification": "0.1-draft.8"` and its claim names the revision.
