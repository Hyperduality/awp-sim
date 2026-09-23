"""Record each session's wire traffic as a trace in the spec's JSON Lines format."""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Hashable
from pathlib import Path
from typing import IO, Any

from awp.jsonrpc import Message

from .audit import redact


class TraceRecorder:
    """Writes `<directory>/<session_id>.jsonl`, with credentials redacted as in the audit log.

    Messages exchanged before a connection has a session (initialize, a failed open, the
    reconnect before session.resume) are held until it does, then written ahead of the session's
    own traffic.
    """

    PENDING_LIMIT = 256
    OPEN_FILES = 64

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._pending: dict[Hashable, list[dict[str, Any]]] = {}
        self._files: OrderedDict[str, IO[str]] = OrderedDict()

    def record(
        self,
        conn: Hashable,
        session: str | None,
        sender: str,
        msg: Message,
        *,
        delivered: bool = True,
    ) -> None:
        method = msg.get("method") or ("error" if "error" in msg else "result")
        line: dict[str, Any] = {"from": sender, "step": method, "msg": redact(msg)}
        if not delivered:
            line["delivered"] = False
        if session is None:
            held = self._pending.setdefault(conn, [])
            if len(held) < self.PENDING_LIMIT:
                held.append(line)
            return
        f = self._file(session)
        for held_line in self._pending.pop(conn, []):
            f.write(json.dumps(held_line, separators=(",", ":")) + "\n")
        f.write(json.dumps(line, separators=(",", ":")) + "\n")
        f.flush()

    def forget(self, conn: Hashable) -> None:
        self._pending.pop(conn, None)

    def close(self) -> None:
        for f in self._files.values():
            f.close()
        self._files.clear()

    def _file(self, session: str) -> IO[str]:
        f = self._files.pop(session, None)
        if f is None:
            f = (self.directory / f"{session}.jsonl").open("a", encoding="utf-8")
            while len(self._files) >= self.OPEN_FILES:
                self._files.popitem(last=False)[1].close()
        self._files[session] = f  # most recently used last
        return f
