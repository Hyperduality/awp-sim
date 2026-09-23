"""The per-session audit log (spec/safety/audit-log): JSON Lines, redacted, hash-chained."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import IO, Any

from awp.jsonrpc import Message

CREDENTIAL_KEYS = frozenset({"session_token", "transfer_token", "snapshot_token"})


def redacted(value: Any) -> str:
    digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return f"[redacted:sha256:{digest[:8]}]"


def redact(msg: Message, paths: Iterable[str] = ()) -> Message:
    """Replace credentials and `redact_paths` values, relative to params or result (AWP-AUD-006)."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: redacted(v) if k in CREDENTIAL_KEYS else walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    out: Message = walk(msg)
    for pointer in paths:
        for root in ("params", "result"):
            _redact_pointer(out.get(root), pointer)
    return out


def _redact_pointer(node: Any, pointer: str) -> None:
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.lstrip("/").split("/")]
    for part in parts[:-1]:
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return
    last = parts[-1]
    if isinstance(node, dict) and last in node:
        node[last] = redacted(node[last])
    elif isinstance(node, list) and last.isdigit() and int(last) < len(node):
        node[int(last)] = redacted(node[int(last)])


class AuditLog:
    """Writes `<directory>/<session_id>.jsonl`, one audit record per line (AWP-AUD-001..007).

    Frames are recorded as their header plus the SHA-256 of the payload, so a log is an audit
    record, not a replay bundle. With the inline binding frames travel as control messages; they
    are still logged by hash, as frames on any other channel would be.
    """

    def __init__(self, directory: Path | str, *, redact_paths: Iterable[str] = ()) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.redact_paths = list(redact_paths)
        self._files: dict[str, IO[str]] = {}
        self._prev: dict[str, str] = {}

    def open(self, session_id: str, header: dict[str, Any]) -> None:
        self._files[session_id] = (self.directory / f"{session_id}.jsonl").open(
            "a", encoding="utf-8"
        )
        self._write(
            session_id,
            {
                "class": "audit_record",
                "ts_mono_ns": 0,
                "direction": "world",
                "kind": "header",
                "body": header,
            },
        )

    def record(self, session_id: str, ts_mono_ns: int, direction: str, msg: Message) -> None:
        if session_id not in self._files:
            return
        if msg.get("method") in ("obs.frame", "cmd.frame"):
            params = msg["params"]
            payload = params.get("payload_b64", "").encode()
            body: dict[str, Any] = {k: v for k, v in params.items() if k != "payload_b64"}
            body["method"] = msg["method"]
            body["payload_sha256"] = hashlib.sha256(payload).hexdigest()
            kind = "frame"
        else:
            body = redact(msg, self.redact_paths)
            kind = "message"
        self._write(
            session_id,
            {"ts_mono_ns": ts_mono_ns, "direction": direction, "kind": kind, "body": body},
        )

    def close(self, session_id: str) -> None:
        f = self._files.pop(session_id, None)
        self._prev.pop(session_id, None)
        if f is not None:
            f.close()

    def close_all(self) -> None:
        for session_id in list(self._files):
            self.close(session_id)

    def _write(self, session_id: str, record: dict[str, Any]) -> None:
        record["prev_hash"] = self._prev.get(session_id)
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        self._prev[session_id] = hashlib.sha256(line.encode()).hexdigest()
        f = self._files[session_id]
        f.write(line + "\n")
        f.flush()


def verify_chain(lines: Iterable[str]) -> bool:
    """True if every record's prev_hash is the SHA-256 of the line before it (AWP-AUD-003)."""
    prev: str | None = None
    for line in lines:
        record = json.loads(line)
        if record.get("prev_hash") != prev:
            return False
        prev = hashlib.sha256(line.rstrip("\n").encode()).hexdigest()
    return True
