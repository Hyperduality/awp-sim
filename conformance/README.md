# Conformance

![AWP: Core World (lockstep), AWP-conformant against 0.1-draft.7](https://img.shields.io/badge/AWP-Core_World_%28lockstep%29%2C_conformant_0.1--draft.7-555)

| Class | Configuration | Claim | Report |
|---|---|---|---|
| Core World | `awp-sim serve --mode lockstep` | Core World (lockstep): AWP-conformant against 0.1-draft.7 (awp-conformance 0.1.0a1) | [`core-world-lockstep.json`](core-world-lockstep.json) |

The report was produced by awp-conformance 0.1.0a1 against awp-python 0.1.0a1, both installed from PyPI: no failure, nothing untested, and five `manual` rows, whose evidence follows (AWP-CNF-005). Every other configuration remains self-assessed against 0.1-draft.7.

## Reproduce

```bash
pip install --pre awp-python awp-conformance
curl -LO https://raw.githubusercontent.com/Hyperduality/awp-conformance/v0.1.0a1/fixtures/awp-sim.json
awp-sim serve --mode lockstep &
export AWP_SIM_PID=$!
awp-conformance world ws://127.0.0.1:8710 --fixture awp-sim.json --out report/
```

The fixture's e-stop hooks signal `$AWP_SIM_PID`; without it the e-stop test is skipped and AWP-EVT-002 is reported untested. The tests cited below run in this repository's CI:

```bash
uv run pytest tests/test_frames.py tests/test_evidence.py tests/test_world.py::test_lockstep_core_flow
```

## Manual evidence

### AWP-DAT-002 — latest-wins senders drop, not queue

- **Drop policy.** Each connection's send queue (`_Outbox`, [`src/awp_sim/server.py`](../src/awp_sim/server.py)) holds at most one unsent frame per latest-wins channel and replaces it when a newer one is produced; control messages and reliable frames queue in order. A peer that stops reading is disconnected at 10,000 pending items rather than buffered without limit.
- **Stalled-receiver test.** `test_a_stalled_receiver_holds_one_frame_per_latest_wins_channel`: 1,000 `proprio` frames and 10 `arm_state` frames are produced while nothing drains the queue; one `proprio` frame, the newest, and all 10 `arm_state` frames remain, in order.
- **Lockstep.** Every channel is per-tick, and AWP-TIM-003 requires a frame carrying each advance's tick on every subscribed channel before the next advance is accepted. A newer frame is therefore produced only by an advance accepted after the older frame was handed to the send queue, so lockstep frames are never marked replaceable and none is dropped. `test_lockstep_core_flow` checks one `proprio` frame per tick.

### AWP-DAT-008 — frame test vectors

`test_vectors` (`tests/test_frames.py`) decodes every vector in `schemas/test-vectors/frames.json` of the `spec/` submodule, pinned at `spec-v0.1-draft.7`: the 10 valid vectors to the listed fields, and the 7 marked `expect_error` rejected with the listed error. `test_roundtrip_without_vendor` re-encodes the 8 valid vectors without an unknown extension or reserved bits byte for byte.

### AWP-ENV-003 — envelope violations during execution

awp-sim models no external disturbance: the arm is kinematic and moves only as commanded. In the claimed configuration motion comes only from `move_to_pose`, whose target and `max_velocity_mps` are checked against the envelope at admission (`command_check`, `reject`), and from stops and aborts, which decelerate along the current segment and never past its end. A straight segment between two points inside the box stays inside it, so no violation can occur during execution, and the world does not monitor for one.

`test_motion_never_leaves_the_envelope` drives 400 seeded random submissions, replacements, queued moves, stops, and cancels in each time model, and checks the arm's position and speed against the envelope after every advance.

The argument covers the claimed configuration only. With `--features servo` (streaming), a stop during servo motion toward a wall can carry the arm past it; that configuration is not claimed.

### AWP-UNI-001 — SI units and unit suffixes

The fields awp-sim defines, reviewed:

| Field | Where | Unit |
|---|---|---|
| `p_m` | `proprio` payload | m |
| `v_mps` | `proprio` payload; `servo_arm` setpoints | m/s |
| `target_m` | `arm_state` payload | m |
| `max_velocity_mps` | `move_to_pose` params | m/s |
| `phase`, `action_id` | `arm_state` payload | not physical |

Every other field is defined by the specification's schemas (`pose`, `aabb_m`, `max_abort_ms`, and so on). `test_world_defined_fields_carry_si_suffixes` collects every action parameter, channel schema field, and payload field across every feature, and fails on any field not reviewed as non-physical that lacks an SI suffix.

### AWP-VER-009 — the draft revision is named

The [README](../README.md) and the [awp-sim page](https://www.agentworldprotocol.com/adapters/awp-sim) name `0.1-draft.7`; the `spec/` submodule is pinned at the tag `spec-v0.1-draft.7`; the report records `"specification": "0.1-draft.7"` and its claim names the revision.
