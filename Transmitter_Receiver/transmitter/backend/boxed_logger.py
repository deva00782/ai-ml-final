"""Receiver-style boxed + structured console logging for the transmitter.

Replicates the ``receiver.py`` terminal output format:

    64-wide box sections (SYSTEM STARTUP / UART CONNECTION / COMMAND RECEIVED /
    COMMAND EXECUTION / ... ACKNOWLEDGEMENT / RESULT) printed straight to
    stdout, interleaved with structured log lines like::

        08:44:54.525Z | INFO    | [receiver.uart] Loaded UART configuration

Box geometry (verified against receiver output):
    + 62 x ('-') + -> width 64
    | <label padded> : <value> padded -> 64 chars
"""

import atexit
import datetime
import logging
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

INNER = 60
HORIZONTAL = "\u2500" * (INNER + 2)  # ─

_TOP = "\u250C"  # ┌
_SEP_L = "\u251C"  # ├
_SEP_R = "\u2524"  # ┤
_BOT_L = "\u2514"  # └
_BOT_R = "\u2518"  # ┘
_VBAR = "\u2502"  # │
_TR = "\u2510"  # ┐


def _row(text=""):
    return f"{_VBAR} {text.ljust(INNER)} {_VBAR}"


def _ensure_utf8():
    """Force UTF-8 on Windows consoles so box-drawing chars survive cp1252."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def now_str():
    """Local timestamp with milliseconds, receiver style (2026-09-21 14:14:53.460)."""
    dt = datetime.datetime.now()
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S')}.{dt.microsecond // 1000:03d}"


def new_cmd_id():
    """Short command trace id, receiver style (cmd_5296a195)."""
    return f"cmd_{uuid.uuid4().hex[:8]}"


def print_box(title, rows=None):
    """Render a 64-wide box section to stdout (receiver format).

    ``rows`` accepts:
      - tuples ``(label, value)``  -> ``| Label       : value             |``
      - dicts  ``{label: value}``  -> same
      - ``None``                   -> blank line
      - plain strings              -> raw line inside the box
    """
    lines = [
        _TOP + HORIZONTAL + _TR,
        _row(str(title)),
        _SEP_L + HORIZONTAL + _SEP_R,
    ]
    if rows:
        for row in rows:
            if row is None:
                lines.append(_row(""))
            elif isinstance(row, dict):
                for k, v in row.items():
                    lines.append(_row(f"{str(k).ljust(11)}: {v}"))
            elif isinstance(row, tuple):
                k, v = row
                lines.append(_row(f"{str(k).ljust(11)}: {v}"))
            else:
                lines.append(_row(str(row)))
    lines.append(_BOT_L + HORIZONTAL + _BOT_R)
    text = "\n".join(lines)
    _ensure_utf8()
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("utf-8", errors="replace").decode("ascii", errors="replace"), flush=True)
    return text


class StructuredFormatter(logging.Formatter):
    """Format log records like the receiver's structured lines::

        08:44:54.525Z | INFO    | [component] message
    """

    def format(self, record):
        try:
            t = time.strftime(
                "%H:%M:%S", time.gmtime(record.created)
            ) + f".{int(record.created * 1000) % 1000:03d}Z"
        except Exception:
            t = time.strftime("%H:%M:%S") + ".000Z"
        level = str(record.levelname).ljust(8)
        try:
            msg = record.getMessage()
        except Exception:
            msg = record.msg
        return f"{t} | {level} | {msg}"


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_LOG_LOCK = threading.Lock()
_LOG_FILES = []
_TEE_INSTALLED = False

_DEFAULT_LOG_PATH = Path(__file__).resolve().parents[1] / "terminal.log"
_LOCAL_LOG_PATH = Path(__file__).resolve().parents[1] / "terminal.log"


class TeeStream:
    """Tees stream output to both the console stream and log file(s)."""

    def __init__(self, original_stream, log_file_handles, lock):
        self.original_stream = original_stream
        self.log_file_handles = log_file_handles
        self.lock = lock

    def write(self, data):
        try:
            self.original_stream.write(data)
            self.original_stream.flush()
        except Exception:
            pass

        if data:
            if isinstance(data, bytes):
                text = data.decode("utf-8", errors="replace")
            else:
                text = str(data)
            clean_text = _ANSI_RE.sub("", text)
            try:
                with self.lock:
                    for fh in self.log_file_handles:
                        try:
                            fh.write(clean_text)
                            fh.flush()
                        except Exception:
                            pass
            except Exception:
                pass

    def flush(self):
        try:
            self.original_stream.flush()
        except Exception:
            pass
        try:
            with self.lock:
                for fh in self.log_file_handles:
                    try:
                        fh.flush()
                    except Exception:
                        pass
        except Exception:
            pass

    def isatty(self):
        return getattr(self.original_stream, "isatty", lambda: False)()

    def fileno(self):
        return getattr(self.original_stream, "fileno", lambda: 1)()

    def reconfigure(self, **kwargs):
        if hasattr(self.original_stream, "reconfigure"):
            return self.original_stream.reconfigure(**kwargs)

    def __getattr__(self, name):
        return getattr(self.original_stream, name)


def setup_terminal_logger(log_paths=None, max_bytes=10 * 1024 * 1024):
    """Enable full terminal output capture to terminal.log.

    Captures stdout, stderr, all logger calls, and print_box output.
    Idempotent.
    """
    global _TEE_INSTALLED, _LOG_FILES
    if _TEE_INSTALLED:
        return get_log_path()

    _ensure_utf8()

    paths = log_paths or [_DEFAULT_LOG_PATH, _LOCAL_LOG_PATH]
    handles = []

    for p in paths:
        target = Path(p)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file() and target.stat().st_size > max_bytes:
                backup = target.with_suffix(".log.1")
                try:
                    if backup.is_file():
                        backup.unlink()
                    target.rename(backup)
                except Exception:
                    pass

            fh = open(target, "a", encoding="utf-8", errors="replace")
            handles.append(fh)
            _LOG_FILES.append(fh)
        except Exception as e:
            sys.stderr.write(f"[WARN] Failed to open log file {target}: {e}\n")

    if handles:
        if not isinstance(sys.stdout, TeeStream):
            sys.stdout = TeeStream(sys.stdout, handles, _LOG_LOCK)
        if not isinstance(sys.stderr, TeeStream):
            sys.stderr = TeeStream(sys.stderr, handles, _LOG_LOCK)

        _TEE_INSTALLED = True

        def _close_logs():
            for h in _LOG_FILES:
                try:
                    h.flush()
                    h.close()
                except Exception:
                    pass

        atexit.register(_close_logs)

    return get_log_path()


def get_log_path() -> str:
    """Return primary log path for display."""
    return str(_DEFAULT_LOG_PATH)


def configure_console(logger=None):
    """Replace console StreamHandlers with the structured format on ``logger``.

    Keeps non-stream handlers (e.g. the mobile WebLogHandler used to push logs
    to the browser) intact. Idempotent.
    """
    setup_terminal_logger()
    root = logger or logging.getLogger()
    _ensure_utf8()
    root.setLevel(logging.DEBUG if os.environ.get("DEBUG_MODE") else logging.INFO)
    stream_handlers = [
        h for h in root.handlers
        if isinstance(h, logging.StreamHandler) and h.stream in (None, sys.stderr, sys.stdout)
    ]
    for handler in stream_handlers:
        root.removeHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(StructuredFormatter())
    root.addHandler(console)
    return console