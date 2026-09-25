# awp-python

Python implementation of the [Agent World Protocol](https://www.agentworldprotocol.com):

- **`awp`** — the client protocol layer. `ClientConnection` is the agent side of AWP as a sans-IO state machine; `awp.aio.AsyncClient` drives it over a WebSocket.
- **`awp_sim`** — the reference world. A sans-IO world engine with a simulated arm, a WebSocket server, an audit log, and a scenario suite that records wire traces.

It targets specification revision **`0.1-draft.8`**, pinned as the `spec/` submodule. This is an alpha: the API will change with the draft.

## Status

![AWP: Core World, AWP-conformant against 0.1-draft.8](https://img.shields.io/badge/AWP-Core_World%2C_conformant_0.1--draft.8-555)

The world is **Core World: AWP-conformant against 0.1-draft.8** in both time models: [awp-conformance](https://github.com/Hyperduality/awp-conformance) reports no failure and nothing untested, and [`conformance/`](conformance/README.md) holds the reports and the evidence for their manual rows. The **sim** profile in lockstep and the client as **Core Agent** in both time models are *self-assessed against 0.1-draft.8*: the suite reports no failure and leaves a few requirements untested. Every recorded trace also passes the spec's own checker, which verifies schemas, the action lifecycle table, idempotency, replay, and frame sequencing.

| Implemented | Not implemented |
|---|---|
| Lockstep (`on_tick`, `any_session`) and streaming | `barrier` tick authority |
| Inline and `ws` stream bindings; binary frame codec (all spec vectors) | Other stream bindings (`webrtc`, `webtransport`, `shm`, `grpc`) |
| Full action lifecycle; preemption (`replace`, `queue`, `reject`, `blend`); idempotency | Standing approvals, grant expiry |
| Watchdog and safe state, heartbeats, resumption with replay and acknowledgement | Multi-bind and shared control (one embodiment) |
| Spatial, velocity, and rate envelopes (`command_check`), monitored during execution | Robotics profile (a simulated arm proves nothing physical) |
| Audit log with redaction and hash chain; e-stop; `world.reset` | |
| Beyond Core (`--features`): task, approval, transfer, seeding, snapshots, replay bundles, a servo command channel | |

## Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/) (Node 20+ only for the trace checker).

```bash
git clone --recurse-submodules https://github.com/Hyperduality/awp-python
cd awp-python
uv sync
```

Run a world, then drive it with the scripted demo agent:

```bash
uv run awp-sim serve                 # streaming world on ws://127.0.0.1:8710
uv run awp-sim demo                  # in another terminal
```

`awp-sim serve --mode lockstep` runs the lockstep world. A token (`--token`, or `$AWP_SIM_TOKEN`) is required on any non-loopback bind, as is TLS (`--tls-cert`, `--tls-key`). Audit logs go to `./awp-audit`; `--record-dir` also writes each session's wire trace. `kill -USR1` engages the e-stop and `kill -USR2` releases it.

Run the failure scenarios and check their traces against the spec:

```bash
uv run awp-sim scenarios --out traces
uv run python scripts/check_traces.py traces/*.jsonl
```

## Using the client

With asyncio:

```python
from awp import ClientConnection
from awp.aio import AsyncClient

conn = ClientConnection({"name": "my-agent", "version": "0.1.0", "vendor": "me"}, ["proprio/json"])
async with AsyncClient(conn, "ws://127.0.0.1:8710") as client:
    await client.initialize()
    await client.open_session("streaming", embodiment="arm_01", subscribe=["proprio"])
    await client.wait_for(lambda e: "proprio" in client.latest)
    record = await client.submit(
        "move_to_pose",
        {"pose": {"frame": "base", "p_m": [0.3, 0.2, 0.5], "q": [0, 0, 0, 1]}},
        basis=client.latest["proprio"].frame,  # the observation this intent rests on
        valid_for_ms=200,
    )
    print((await client.wait_terminal(record.action_id)).state)
    await client.close_session()
```

Without it, feed `ClientConnection` decoded messages and send what it queues:

```python
events = conn.receive(message)  # typed events: ActionUpdated, FrameReceived, ...
for out in conn.outgoing():  # messages to send, in order
    transport.send(json.dumps(out))
```

The connection tracks the action lifecycle against the spec's transition table, deduplicates replayed statuses, keeps the clock offset from heartbeats, and reports any violation by the world as a `ProtocolViolation` event.

## Layout

```
src/awp/            client protocol layer (sans-IO), asyncio adapter, frame codec, schemas
src/awp/_spec/      schemas and lifecycle table bundled from spec/ (scripts/sync_spec.py)
src/awp_sim/        world engine, arm, server, audit log, loopback, scenarios, CLI
spec/               agent-world-protocol, pinned at spec-v0.1-draft.8
scripts/            spec sync and trace checking
```

## Development

```bash
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest --cov
uv run python scripts/sync_spec.py --check
```

To move to a new draft revision: check out its tag in `spec/`, run `scripts/sync_spec.py`, update `SPEC_REVISION` in `src/awp/__init__.py`, and fix what the tests report.

## License

Apache-2.0. See [LICENSE](LICENSE).
