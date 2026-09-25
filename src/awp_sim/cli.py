"""The `awp-sim` command."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import ssl
import sys
from pathlib import Path

from . import __version__, scenarios
from .audit import AuditLog
from .config import FEATURES, WorldConfig
from .server import Server
from .world import World


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="awp-sim", description="Agent World Protocol reference world.")
    p.add_argument("--version", action="version", version=f"awp-sim {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the reference world")
    s.add_argument("--mode", choices=["streaming", "lockstep"], default="streaming")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8710)
    s.add_argument(
        "--token",
        default=os.environ.get("AWP_SIM_TOKEN"),
        help="require this bearer token (default: $AWP_SIM_TOKEN)",
    )
    s.add_argument("--tls-cert", type=Path)
    s.add_argument("--tls-key", type=Path)
    s.add_argument(
        "--insecure", action="store_true", help="allow a non-loopback bind without a token or TLS"
    )
    s.add_argument(
        "--audit-dir",
        type=Path,
        default=Path("awp-audit"),
        help="per-session audit logs (AWP-AUD-001; default: ./awp-audit)",
    )
    s.add_argument(
        "--no-audit",
        action="store_true",
        help="disable the audit log (not conformant; for development only)",
    )
    s.add_argument("--record-dir", type=Path, help="write per-session wire traces here")
    s.add_argument(
        "--replay-dir",
        type=Path,
        help="write replay bundles instead of audit records (lockstep, --features sim)",
    )
    s.add_argument(
        "--features",
        default="",
        help=f"comma-separated features beyond Core: {', '.join(sorted(FEATURES))}",
    )
    s.add_argument(
        "--approver-token",
        default=os.environ.get("AWP_SIM_APPROVER_TOKEN"),
        help="bearer token of approver connections (with --features approval)",
    )
    s.add_argument("--approval-timeout-ms", type=int, default=60000)
    s.add_argument(
        "--stream-binding",
        choices=["inline", "ws"],
        default="inline",
        help="offer frames on a ws stream connection as well as inline (AWP-TRN-003)",
    )
    for name, default in (
        ("watchdog-ms", 2000),
        ("heartbeat-ms", 5000),
        ("reconnect-window-ms", 30000),
        ("tick-ms", 20),
        ("max-duration-ms", 10000),
    ):
        s.add_argument(f"--{name}", type=int, default=default)
    s.add_argument("--log-level", default="INFO")

    sc = sub.add_parser("scenarios", help="run the scripted scenarios")
    sc.add_argument("names", nargs="*", help="scenarios to run (default: all)")
    sc.add_argument("--list", action="store_true", help="list scenarios and exit")
    sc.add_argument("--out", type=Path, help="write traces and report.json here")

    r = sub.add_parser("replay", help="replay a replay bundle and compare (AWP-REP-003)")
    r.add_argument("bundle", type=Path)

    m = sub.add_parser("manifest", help="print the world manifest")
    m.add_argument("--mode", choices=["streaming", "lockstep"], default="streaming")
    m.add_argument("--features", default="")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "manifest":
        features = frozenset(f for f in args.features.split(",") if f)
        print(json.dumps(WorldConfig(mode=args.mode, features=features).manifest(), indent=2))
        return 0
    if args.command == "scenarios":
        return _scenarios(args)
    if args.command == "replay":
        from .replay import replay

        outcome = replay(args.bundle)
        if outcome.reproduced:
            print(f"reproduced {outcome.frames} frames and {outcome.transitions} transitions")
            return 0
        print(f"not reproduced: {outcome.difference}", file=sys.stderr)
        return 1
    return _serve(args)


def _scenarios(args: argparse.Namespace) -> int:
    if args.list:
        for name, (description, _) in scenarios.SCENARIOS.items():
            print(f"{name:24} {description}")
        return 0
    unknown = [n for n in args.names if n not in scenarios.SCENARIOS]
    if unknown:
        print(f"unknown scenario(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    results = scenarios.run_all(args.names or None)
    for r in results:
        metrics = " ".join(f"{k}={v}" for k, v in r.metrics.items())
        print(f"{'PASS' if r.passed else 'FAIL'}  {r.name:24} {metrics}")
        for check, ok in r.checks:
            if not ok:
                print(f"      ✗ {check}")
        if r.error:
            print(f"      ✗ {r.error}")
    if args.out:
        scenarios.write(results, args.out)
        print(f"traces and report written to {args.out}")
    return 0 if all(r.passed for r in results) else 1


def _serve(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        config = _config(args)
    except ValueError as err:
        print(f"awp-sim: {err}", file=sys.stderr)
        return 2
    tls = None
    if args.tls_cert:
        tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        tls.load_cert_chain(args.tls_cert, args.tls_key)
    return _run_server(args, config, tls)


def _config(args: argparse.Namespace) -> WorldConfig:
    return WorldConfig(
        mode=args.mode,
        watchdog_ms=args.watchdog_ms,
        heartbeat_interval_ms=args.heartbeat_ms,
        reconnect_window_ms=args.reconnect_window_ms,
        tick_ms=args.tick_ms,
        max_duration_ms=args.max_duration_ms,
        features=frozenset(f for f in args.features.split(",") if f),
        approval_timeout_ms=args.approval_timeout_ms,
    )


def _run_server(args: argparse.Namespace, config: WorldConfig, tls: ssl.SSLContext | None) -> int:
    if args.replay_dir is not None:
        if not config.has("sim"):
            print("awp-sim: --replay-dir needs --mode lockstep --features sim", file=sys.stderr)
            return 2
        audit: AuditLog | None = AuditLog(args.replay_dir, bundle=True)
    else:
        audit = None if args.no_audit else AuditLog(args.audit_dir)
    world = World(config, audit=audit)
    try:
        server = Server(
            world,
            host=args.host,
            port=args.port,
            token=args.token,
            ssl_context=tls,
            allow_insecure=args.insecure,
            record_dir=args.record_dir,
            stream_binding=args.stream_binding == "ws",
            approver_token=args.approver_token,
        )
    except ValueError as err:
        print(f"awp-sim: {err}", file=sys.stderr)
        return 2

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        # Operator e-stop: SIGUSR1 engages, SIGUSR2 releases (AWP-EVT-002).
        for name, handler in (("SIGUSR1", server.engage_estop), ("SIGUSR2", server.release_estop)):
            usr = getattr(signal, name, None)
            if usr is not None:
                loop.add_signal_handler(usr, handler)
        async with server:
            await stop.wait()

    try:
        asyncio.run(run())
    finally:
        if audit is not None:
            audit.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
