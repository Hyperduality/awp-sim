# awp-sim

The reference world for the [Agent World Protocol](https://www.agentworldprotocol.com). It has a simulated arm, optionally a gripper, and runs in either time model. It exists to exercise the protocol, not to model physics.

It is built from these parts:

- a sans-IO world engine;
- a WebSocket server;
- an audit log;
- a scenario suite that records wire traces.

It speaks AWP through the protocol layer of [awp-python](https://github.com/Hyperduality/awp-python): its message codec, frame codec, schemas, and lifecycle table.

It targets specification revision **`0.1-draft.9`**, pinned as the `spec/` submodule. This is an alpha, so it will change along with the draft.

## Status

![AWP: Core World, AWP-conformant against 0.1-draft.9](https://img.shields.io/badge/AWP-Core_World%2C_conformant_0.1--draft.9-555)

The world is **Core World: AWP-conformant against 0.1-draft.9** in both time models, and so is every feature it offers. [awp-conformance](https://github.com/Hyperduality/awp-conformance) reports no failure and nothing untested. [`conformance/`](conformance/README.md) holds the reports and the evidence for their manual rows, and CI runs the suite on every change. Every recorded trace also passes the spec's own checker, which verifies:

- schemas;
- the action lifecycle table;
- idempotency;
- replay;
- frame sequencing.

Implemented:

- lockstep (`on_tick`; `any_session`, or `barrier` with the gripper) and streaming;
- inline and `ws` stream bindings;
- the full action lifecycle, preemption (`replace`, `queue`, `reject`, `blend`), and idempotency;
- watchdog and safe state, heartbeats, and resumption with replay and acknowledgement;
- spatial, velocity, and rate envelopes (`command_check`), monitored during execution;
- the audit log with redaction and hash chain, the e-stop, and `world.reset`;
- beyond Core (`--features`): task, approval and standing approvals, transfer, seeding, snapshots, replay bundles, a servo command channel, and a gripper.

Not implemented:

- grant expiry;
- other stream bindings (`webrtc`, `webtransport`, `shm`, `grpc`);
- the robotics profile (a simulated arm proves nothing physical).

## Quickstart

```bash
pip install --pre awp-sim
awp-sim serve                        # streaming world on ws://127.0.0.1:8710
awp-demo                             # in another terminal: awp-python's demo agent
```

Other ways to run it:

- **Lockstep:** `awp-sim serve --mode lockstep` runs the lockstep world.
- **Remote access:** any non-loopback bind requires a token (`--token`, or `$AWP_SIM_TOKEN`) and TLS (`--tls-cert`, `--tls-key`). Pass the same token to the agent with `awp-demo --token`.
- **Logs:** audit logs go to `./awp-audit`, and `--record-dir` also writes each session's wire trace.
- **E-stop:** `kill -USR1` engages the e-stop and `kill -USR2` releases it.

Run the failure scenarios:

```bash
awp-sim scenarios --out traces
```

## The gripper

`--features gripper` adds `gripper_01`, with one action type, `gripper_move { width_m }`, and one channel, `gripper_state`.

- It shares the multi-bind group `cell` with the arm. A session binds both with `embodiments: ["arm_01", "gripper_01"]`, and its submissions then name `embodiment_id`.
- It is shared: several sessions can bind it at once. Whichever submits a `gripper_move` first has the gripper until that action ends. Meanwhile the others' submissions are refused with `AWP_BUSY`.
- In lockstep, the tick authority becomes `barrier`. The world advances once every session bound to an embodiment has a `world.tick` pending, and answers each call after its `count` advances.
  - While a session's call is pending, a second one is refused. A call is lost with its connection, and the barrier then waits for a new one from the resumed session.
  - A reset or restore refuses pending calls with `AWP_TICK_MISMATCH`.

## Layout

```
src/awp_sim/        world engine, arm and gripper, server, audit log, loopback, scenarios, replay, CLI
tests/              engine, server, feature, scenario, and evidence tests
conformance/        conformance reports and evidence
spec/               agent-world-protocol, pinned at spec-v0.1-draft.9
scripts/            trace checking against the spec's checker
```

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Node 20+ is needed only for the trace checker.

```bash
git clone --recurse-submodules https://github.com/Hyperduality/awp-sim
cd awp-sim
uv sync
```

[AGENTS.md](AGENTS.md) lists the checks and the steps for moving to a new draft revision and for releasing.

## License

Apache-2.0. See [LICENSE](LICENSE).
