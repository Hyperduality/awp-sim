# awp-sim

The reference world for the [Agent World Protocol](https://www.agentworldprotocol.com). It has one simulated arm and runs in either time model. It exists to exercise the protocol, not to model physics.

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

| Implemented | Not implemented |
|---|---|
| Lockstep (`on_tick`, `any_session`) and streaming | `barrier` tick authority |
| Inline and `ws` stream bindings | Other stream bindings (`webrtc`, `webtransport`, `shm`, `grpc`) |
| Full action lifecycle; preemption (`replace`, `queue`, `reject`, `blend`); idempotency | Grant expiry |
| Watchdog and safe state, heartbeats, resumption with replay and acknowledgement | Multi-bind and shared control (one embodiment) |
| Spatial, velocity, and rate envelopes (`command_check`), monitored during execution | Robotics profile (a simulated arm proves nothing physical) |
| Audit log with redaction and hash chain; e-stop; `world.reset` | |
| Beyond Core (`--features`): task, approval and standing approvals, transfer, seeding, snapshots, replay bundles, a servo command channel | |

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

## Layout

```
src/awp_sim/        world engine, arm, server, audit log, loopback, scenarios, replay, CLI
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
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest --cov
uv run awp-sim scenarios --out traces && uv run python scripts/check_traces.py traces/*.jsonl
```

The world tracks the draft revision of awp-python. To move to a new one:

1. Check out its tag in `spec/`.
2. Update the `awp-python` requirement.
3. Fix whatever the tests report.

## License

Apache-2.0. See [LICENSE](LICENSE).
