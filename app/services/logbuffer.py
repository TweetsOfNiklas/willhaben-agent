"""In-memory ring buffer of recent log records, for the bug report (app/services/bugreport.py).

Nothing is written to disk and nothing leaves the process: the records only show up when the owner creates a
bug report, and they are anonymised there. Warnings and errors (with tracebacks) are kept in their own buffer, so
chatty INFO logs never push them out.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

WARN_MAX = 300
INFO_MAX = 500
MSG_MAX = 2000
TB_MAX = 8000
STARTED = time.time()          # process start (close enough: imported while the app module loads)

_fmt = logging.Formatter()


class RingBufferHandler(logging.Handler):
    def __init__(self, warn_max: int = WARN_MAX, info_max: int = INFO_MAX) -> None:
        super().__init__(level=logging.INFO)
        self.warnings: deque[dict[str, Any]] = deque(maxlen=warn_max)
        self.infos: deque[dict[str, Any]] = deque(maxlen=info_max)
        self._mutex = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string must not break logging
            msg = str(record.msg)
        tb = None
        if record.exc_info and record.exc_info[0] is not None:
            try:
                tb = _fmt.formatException(record.exc_info)[-TB_MAX:]
            except Exception:  # noqa: BLE001
                tb = None
        entry = {"ts": datetime.fromtimestamp(record.created, timezone.utc), "level": record.levelname,
                 "logger": record.name, "message": msg[:MSG_MAX], "traceback": tb}
        with self._mutex:
            (self.warnings if record.levelno >= logging.WARNING else self.infos).append(entry)

    def records(self, include_info: bool = False) -> list[dict[str, Any]]:
        with self._mutex:
            out = list(self.warnings) + (list(self.infos) if include_info else [])
        return sorted(out, key=lambda e: e["ts"])

    def clear(self) -> None:
        with self._mutex:
            self.warnings.clear()
            self.infos.clear()


_handler: RingBufferHandler | None = None


def install() -> RingBufferHandler:
    """Attach the buffer to the root logger once (idempotent)."""
    global _handler
    if _handler is None:
        _handler = RingBufferHandler()
        logging.getLogger().addHandler(_handler)     # level stays as configured (main.py: INFO)
    return _handler


def records(include_info: bool = False) -> list[dict[str, Any]]:
    return _handler.records(include_info) if _handler else []
