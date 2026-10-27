import sys
import time
import traceback
import threading
from pathlib import Path

# Force UTF-8 stdout/stderr encoding on Windows consoles to prevent charmap codec errors
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

LOG_FILE_PATH = Path(__file__).resolve().parent / "system_execution.log"

class SystemLogger:
    def __init__(self, socketio_emitter=None):
        self.socketio_emitter = socketio_emitter
        self._lock = threading.Lock()
        with open(LOG_FILE_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n--- LOG SESSION INITIALIZED AT {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")

    def set_emitter(self, emitter):
        with self._lock:
            self.socketio_emitter = emitter

    def _format(self, level, module, message):
        time_str = time.strftime("%H:%M:%S")
        return f"[{time_str}] [{level}] [{module}] {message}"

    def _write_to_file(self, formatted_message):
        with self._lock:
            try:
                with open(LOG_FILE_PATH, "a", encoding="utf-8", errors="replace") as f:
                    f.write(formatted_message + "\n")
                    f.flush()
            except Exception:
                pass

    def _safe_print(self, msg, stream=sys.stdout):
        try:
            print(msg, file=stream, flush=True)
        except UnicodeEncodeError:
            clean_msg = msg.encode('ascii', 'replace').decode('ascii')
            print(clean_msg, file=stream, flush=True)

    def info(self, module, message):
        formatted = self._format("INFO", module, message)
        self._safe_print(f"\033[94m{formatted}\033[0m")
        self._write_to_file(formatted)
        self._emit(formatted)

    def debug(self, module, message):
        formatted = self._format("DEBUG", module, message)
        self._safe_print(f"\033[90m{formatted}\033[0m")
        self._write_to_file(formatted)
        self._emit(formatted)

    def success(self, module, message):
        formatted = self._format("SUCCESS", module, message)
        self._safe_print(f"\033[92m{formatted}\033[0m")
        self._write_to_file(formatted)
        self._emit(formatted)

    def warn(self, module, message):
        formatted = self._format("WARN", module, message)
        self._safe_print(f"\033[93m{formatted}\033[0m")
        self._write_to_file(formatted)
        self._emit(formatted)

    def error(self, module, message, exc=None):
        formatted = self._format("ERROR", module, message)
        if exc:
            formatted += f"\n[Traceback]\n{traceback.format_exc()}"
        self._safe_print(f"\033[91m{formatted}\033[0m", stream=sys.stderr)
        self._write_to_file(formatted)
        self._emit(formatted)

    def critical(self, module, message):
        formatted = self._format("CRITICAL", module, message)
        self._safe_print(f"\033[95m{formatted}\033[0m", stream=sys.stderr)
        self._write_to_file(formatted)
        self._emit(formatted)

    def _emit(self, formatted_message):
        with self._lock:
            if self.socketio_emitter:
                try:
                    self.socketio_emitter(formatted_message)
                except Exception:
                    pass

# Singleton Logger Instance
logger = SystemLogger()
