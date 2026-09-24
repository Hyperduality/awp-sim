# awp-python

Python implementation of the [Agent World Protocol](https://www.agentworldprotocol.com):

- **`awp`** — the client protocol layer. `ClientConnection` is the agent side of AWP as a sans-IO state machine; `awp.aio.AsyncClient` drives it over a WebSocket.
- **`awp_sim`** — the reference world. A sans-IO world engine with a simulated arm, a WebSocket server, an audit log, and a scenario suite that records wire traces.

It targets specification revision **`0.1-draft.6`**, pinned as the `spec/` submodule. This is an alpha: the API will change with the draft.

## Status

The world and client target **Core World** and **Core Agent** for both time models on the inline binding, *self-assessed against 0.1-draft.6*: [awp-conformance](https://github.com/Hyperduality/awp-conformance) reports no failure against either, and leaves some requirements untested. Every recorded trace also passes the spec's own checker, which verifies schemas, the action lifecycle table, idempotency, replay, and frame sequencing.

| Implemented | Not implemented (not required by Core) |
|---|---|
| Lockstep (`on_tick`, `any_session`) and streaming | Approval, command channels, task |
| Inline binding; binary frame codec (all spec vectors) | Stream bindings other than inline |
| Full action lifecycle, preemption (`replace`, `queue`, `reject`), idempotency | `blend` preemption |
| Watchdog and safe state, heartbeats, resumption with replay and acknowledgement | Snapshots, restore, replay bundles |
| Spatial, velocity, and rate envelopes (`command_check`) | Multi-bind, transfer, shared control |
| Audit log with redaction and hash chain; e-stop; `world.reset` | Robotics profile (a simulated arm proves nothing physical) |

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
spec/               agent-world-protocol, pinned at spec-v0.1-draft.6
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
