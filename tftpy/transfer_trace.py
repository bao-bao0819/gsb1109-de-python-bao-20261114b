# vim: ts=4 sw=4 et ai:
"""Structured per-packet transfer tracing for offline diagnosis.

The default tftpy text logs interleave human-readable messages on both sides
of a transfer, which makes post-mortem alignment of a half-finished upload
fragile. This module writes one JSON object per packet event to a JSONL file,
recording both a wall clock timestamp (for cross-side alignment under a known
clock skew bound) and a monotonic timestamp (for unambiguous ordering within a
single process).

A tracer is attached to a TftpContext; when no tracer is attached all tracing
calls are no-ops, so default behaviour and performance are unchanged.

Event schema (one JSON object per line)::

    {"wall": 1763123456.789, "mono": 42123.4, "role": "client",
     "session": "127.0.0.1:54321", "dir": "tx", "opcode": "DAT",
     "block": 6, "size": 516, "attempt": 1, "note": null, "extra": {}}

The files written here are the canonical input for ``tftpy.offline_diag``.
"""

import json
import os
import threading
import time


class TransferTracer:
    """Writes structured packet events to a JSONL file.

    ``role`` is "client" or "server". ``session`` is any stable identifier of
    the transfer, such as the remote ``ip:tid`` key. The writer is thread-safe
    because the server select loop and housekeeping share one tracer.
    """

    def __init__(self, path, role, session=None):
        self.path = path
        self.role = role
        self.session = session
        self._lock = threading.Lock()
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")
        self._closed = False
        self.event(
            "trace",
            "OPEN",
            note="tracer opened",
            pid=os.getpid(),
        )

    def event(self, direction, opcode, block=None, size=None, note=None,
              wall=None, mono=None, **extra):
        """Record one packet-level event.

        ``direction`` is "tx", "rx", "store" or the meta value "trace".
        ``opcode`` is the TFTP packet name (RRQ, WRQ, DAT, ACK, OACK, ERR)
        or "OPEN"/"CLOSE". ``wall``/``mono`` default to the current clocks but
        may be overridden, which keeps offline tooling tests deterministic.
        """
        if self._closed:
            return
        record = {
            "wall": time.time() if wall is None else wall,
            "mono": time.monotonic() if mono is None else mono,
            "role": self.role,
            "session": self.session,
            "dir": direction,
            "opcode": opcode,
            "block": block,
            "size": size,
            "note": note,
        }
        if extra:
            record["extra"] = extra
        line = json.dumps(record, separators=(",", ":"), sort_keys=True)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self):
        """Flush and close the trace file; safe to call repeatedly."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._fh.flush()
            try:
                os.fsync(self._fh.fileno())
            except OSError:
                pass
            self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
