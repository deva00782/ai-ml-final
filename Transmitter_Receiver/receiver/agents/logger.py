"""
Centralized Production Logging & Evaluation Telemetry Engine.

Provides:
- Professional, box-wise event logging for human-readable console and receiver.log.
- 100% single-line JSON-Lines structured logging (receiver.jsonl) with zero box characters.
- Evaluation telemetry logging (evaluation.jsonl) with Member 3 Failure Classification
  and Member 4 Response Validation.
- Thread-safe rotating file loggers preventing disk overflow.
- Clean root-level log synchronization.
"""

import datetime
import json
import logging
import os
import sys
import textwrap
import threading
import traceback
import uuid
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple, Union

_INITIALIZED = False
_INIT_LOCK = threading.Lock()
_ROOT_LOGGER_NAME = "receiver"
_EVAL_LOGGER_INSTANCE: Optional["EvaluationLogger"] = None

BOX_WIDTH = 64
CONFIG_FILE_NAME = "config.json"

# Attributes standard to standard library LogRecord that should not go into extra context
_STANDARD_LOG_RECORD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName", "asctime",
}


def get_base_dir() -> str:
    """Returns the base project directory, supporting both source and PyInstaller."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


# Ensure Windows console encoding handles UTF-8 safely without crashing
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class FailureCategory:
    """Standardized failure categories supported by Member 3."""
    SUCCESS = "SUCCESS"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    DEVICE_TIMEOUT = "DEVICE_TIMEOUT"
    MISFIRE = "MISFIRE"
    DEVICE_ERROR = "DEVICE_ERROR"
    INVALID_COMMAND = "INVALID_COMMAND"


def _classify_from_response(resp: str) -> Optional[str]:
    if resp in ("TIMEOUT", "NO_RESPONSE", "DEVICE_TIMEOUT"):
        return FailureCategory.DEVICE_TIMEOUT
    if resp in ("MISFIRE", "UNINTENDED"):
        return FailureCategory.MISFIRE
    if resp in ("INVALID", "INVALID_COMMAND", "UNRECOGNIZED", "UNKNOWN"):
        return FailureCategory.INVALID_COMMAND
    if resp in ("ERROR", "DEVICE_ERROR", "FAILED", "EXECUTION_FAILED"):
        return FailureCategory.DEVICE_ERROR
    return None


def _classify_from_error(err_str: str) -> str:
    if "TIMEOUT" in err_str or "DEVICE_TIMEOUT" in err_str:
        return FailureCategory.DEVICE_TIMEOUT
    if any(k in err_str for k in ("INVALID", "UNRECOGNIZED", "UNKNOWN")):
        return FailureCategory.INVALID_COMMAND
    if "CONFIDENCE" in err_str:
        return FailureCategory.LOW_CONFIDENCE
    if "MISFIRE" in err_str:
        return FailureCategory.MISFIRE
    return FailureCategory.DEVICE_ERROR


def classify_failure(
    error: Any = None,
    device_response: Optional[str] = None,
    confidence: Optional[float] = None,
) -> str:
    """Classify failure into one of Member 3's standardized categories."""
    resp = str(device_response or "").strip().upper()
    cat = _classify_from_response(resp)
    if cat:
        return cat

    if confidence is not None and confidence < 0.60:
        return FailureCategory.LOW_CONFIDENCE

    if error is not None:
        return _classify_from_error(str(error).upper())

    return FailureCategory.SUCCESS


def validate_response(
    device_status: str,
    tracker_status: str,
    failure_category: str,
    expected_category: Optional[str] = None,
) -> Tuple[str, bool]:
    """Validate device response & tracker status against failure classification (Member 4).

    Returns:
        tuple (validation_str, is_match) where validation_str is '✓ MATCH' or '✗ MISMATCH'.
    """
    if expected_category is None:
        expected_category = failure_category

    dev_failed = str(device_status or "").strip().upper() in ("ERROR", "FAILED", "DEVICE_ERROR")
    tracker_failed = str(tracker_status or "").strip().upper() in ("ERROR", "FAILED", "TRACKER_ERROR")
    if (dev_failed or tracker_failed) and failure_category.strip().upper() == FailureCategory.SUCCESS:
        is_match = False
    else:
        is_match = (failure_category.strip().upper() == expected_category.strip().upper())

    validation_str = "✓ MATCH" if is_match else "✗ MISMATCH"
    return validation_str, is_match


def _format_single_field_row(
    k: Any, v: Any, max_k_len: int, inner_width: int, empty_line: str
) -> List[str]:
    if k == "" and (v is None or v == ""):
        return [empty_line]
    val_str = "" if v is None else str(v)
    key_str = f"{str(k):<{max_k_len}}"
    prefix = f"{key_str} : "
    prefix_len = len(prefix)
    avail_val_width = inner_width - prefix_len

    if len(val_str) <= avail_val_width:
        content = f"{prefix}{val_str}"
        return [f"│ {content:<{inner_width}} │"]

    wrapped = textwrap.wrap(val_str, width=avail_val_width)
    if not wrapped:
        return [f"│ {prefix:<{inner_width}} │"]

    res = [f"│ {prefix + wrapped[0]:<{inner_width}} │"]
    indent = " " * prefix_len
    for w in wrapped[1:]:
        res.append(f"│ {indent + w:<{inner_width}} │")
    return res


def _render_field_rows(
    fields: Union[Dict[str, Any], List[Tuple[str, Any]]],
    inner_width: int,
    empty_line: str,
) -> List[str]:
    """Renders formatted key-value field rows inside a box."""
    rows: List[str] = []
    field_list = list(fields.items()) if isinstance(fields, dict) else list(fields)
    valid_keys = [str(k) for k, _ in field_list if k and not str(k).startswith("__")]
    max_k_len = max([len(k) for k in valid_keys], default=10)
    max_k_len = min(max(max_k_len, 10), 18)

    for item in field_list:
        if isinstance(item, str):
            rows.append(f"│ {item:<{inner_width}} │")
        else:
            k, v = item
            rows.extend(_format_single_field_row(k, v, max_k_len, inner_width, empty_line))
    return rows


def _render_line_rows(lines: List[str], inner_width: int, empty_line: str) -> List[str]:
    """Renders plain text lines inside a box."""
    rows: List[str] = []
    for ln in lines:
        if ln == "":
            rows.append(empty_line)
        else:
            rows.append(f"│ {ln:<{inner_width}} │")
    return rows


def format_box(
    title: str,
    fields: Optional[Union[Dict[str, Any], List[Tuple[str, Any]]]] = None,
    lines: Optional[List[str]] = None,
    width: int = BOX_WIDTH,
) -> str:
    """Renders a clean, professional Unicode box with aligned fields or lines.

    Box width is strictly respected to ensure clean rendering on all terminals.
    """
    inner_width = width - 4  # 2 border chars + 2 padding spaces
    top_border = "┌" + "─" * (width - 2) + "┐"
    divider = "├" + "─" * (width - 2) + "┤"
    bottom_border = "└" + "─" * (width - 2) + "┘"
    empty_line = "│" + " " * (width - 2) + "│"

    title_clean = title.strip()[:inner_width]
    title_row = f"│ {title_clean:<{inner_width}} │"

    rows = [top_border, title_row, divider]

    if fields:
        rows.extend(_render_field_rows(fields, inner_width, empty_line))

    if lines:
        if fields:
            rows.append(empty_line)
        rows.extend(_render_line_rows(lines, inner_width, empty_line))

    rows.append(bottom_border)
    return "\n".join(rows)


def log_box(
    title: str,
    fields: Optional[Any] = None,
    lines: Optional[List[str]] = None,
    level: int = logging.INFO,
    logger: Optional[logging.Logger] = None,
    extra: Optional[Dict[str, Any]] = None,
    command_id: Optional[str] = None,
    command: Optional[str] = None,
    event_name: Optional[str] = None,
    width: int = BOX_WIDTH,
) -> str:
    """Format and emit a box-wise event to human-readable log and machine-readable JSONL."""
    box_str = format_box(title, fields=fields, lines=lines, width=width)
    target_logger = logger or get_logger()

    if isinstance(fields, dict):
        parsed_fields = fields
    elif fields:
        parsed_fields = dict(fields)
    else:
        parsed_fields = {}

    extra_dict: Dict[str, Any] = {
        "is_box": True,
        "box_title": title,
        "box_fields": parsed_fields,
        "box_lines": lines or [],
        "box_event": event_name,
    }
    if command_id:
        extra_dict["command_id"] = command_id
    if command:
        extra_dict["command"] = command
    if extra:
        extra_dict.update(extra)

    target_logger.log(level, box_str, extra=extra_dict)
    return box_str


def _derive_box_event_name(record: logging.LogRecord, box_title: str) -> str:
    event_name = getattr(record, "box_event", None)
    if not event_name:
        clean_title = box_title.replace("—", " ").replace("-", " ")
        words = [w for w in clean_title.split() if w.isalnum()]
        return "_".join(words).upper() if words else "BOX_EVENT"
    return event_name


def _extract_clean_box_fields(box_fields: Any, log_entry: Dict[str, Any]) -> None:
    if not isinstance(box_fields, dict):
        return
    for k, v in box_fields.items():
        if not k or str(k).startswith("__") or v is None or str(v).strip() == "":
            continue
        clean_k = str(k).lower().replace(" ", "_").replace("-", "_").replace(":", "").strip()
        if clean_k and clean_k not in log_entry:
            log_entry[clean_k] = v


def _populate_box_log_entry(record: logging.LogRecord, log_entry: Dict[str, Any]) -> None:
    """Extracts box title, event name, and box fields into the log entry."""
    box_title = getattr(record, "box_title", "")
    log_entry["event"] = _derive_box_event_name(record, box_title)
    log_entry["message"] = box_title or "Box Event"

    box_fields = getattr(record, "box_fields", {})
    _extract_clean_box_fields(box_fields, log_entry)


def _populate_extra_context(record: logging.LogRecord, log_entry: Dict[str, Any]) -> None:
    """Extracts non-standard attributes from the LogRecord into the log entry."""
    extra_context = {
        k: v for k, v in record.__dict__.items()
        if k not in _STANDARD_LOG_RECORD_ATTRS
        and not k.startswith("_")
        and k not in ("is_box", "box_title", "box_fields", "box_lines", "box_event")
    }
    for ek, ev in extra_context.items():
        if ek not in log_entry:
            log_entry[ek] = ev


def _populate_exception_info(record: logging.LogRecord, log_entry: Dict[str, Any]) -> None:
    """Extracts exception type, message, and traceback into the log entry."""
    if record.exc_info and record.exc_info[0] is not None:
        exc_type, exc_val, exc_tb = record.exc_info
        log_entry["exception"] = {
            "type": exc_type.__name__ if exc_type else "Exception",
            "message": str(exc_val),
            "traceback": [
                line.strip() for line in traceback.format_exception(exc_type, exc_val, exc_tb)
            ],
        }


class JSONFormatter(logging.Formatter):
    """Formats log records as structured, single-line JSON objects with zero box characters."""

    def format(self, record: logging.LogRecord) -> str:
        ct = datetime.datetime.fromtimestamp(record.created, tz=datetime.timezone.utc)
        ts_str = ct.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}Z"
        raw_msg = record.getMessage()

        is_box = getattr(record, "is_box", False) or raw_msg.startswith("┌")

        log_entry: Dict[str, Any] = {
            "timestamp": ts_str,
            "level": record.levelname,
            "component": record.name.split(".")[-1] if "." in record.name else record.name,
            "logger": record.name,
            "thread": record.threadName,
            "pid": record.process,
        }

        if is_box:
            _populate_box_log_entry(record, log_entry)
        else:
            log_entry["message"] = raw_msg

        if record.levelno >= logging.WARNING:
            log_entry["location"] = f"{record.filename}:{record.lineno}"

        _populate_extra_context(record, log_entry)
        _populate_exception_info(record, log_entry)

        return json.dumps(log_entry, ensure_ascii=False)


class CleanConsoleFormatter(logging.Formatter):
    """Human-friendly console formatter that outputs clean boxes directly without line truncation."""

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if getattr(record, "is_box", False) or msg.startswith("┌"):
            return msg

        ct = datetime.datetime.fromtimestamp(record.created, tz=datetime.timezone.utc)
        ts = ct.strftime("%H:%M:%S") + f".{int(record.msecs):03d}Z"
        prefix = f"{ts} | {record.levelname:<7} | [{record.name}]"
        main_msg = f"{prefix} {msg}"

        extra_lines = []
        extra_context = {
            k: v for k, v in record.__dict__.items()
            if k not in _STANDARD_LOG_RECORD_ATTRS
            and not k.startswith("_")
            and k not in ("is_box", "box_title", "box_fields", "box_lines", "box_event")
        }
        if extra_context:
            for k, v in extra_context.items():
                extra_lines.append(f"  | {k}: {v}")

        if record.exc_info and record.exc_info[0] is not None:
            exc_type, exc_val, exc_tb = record.exc_info
            tb_lines = traceback.extract_tb(exc_tb)
            last_frame = tb_lines[-1] if tb_lines else None
            frame_info = f"{last_frame.filename}:{last_frame.lineno} in {last_frame.name}" if last_frame else "unknown"

            err_card = [
                f"  +-- [ERROR DIAGNOSTIC: {exc_type.__name__}]",
                f"  | Details: {exc_val}",
                f"  | Source:  {frame_info}",
            ]
            if extra_lines:
                err_card.extend(extra_lines)
            err_card.append("  +-----------------------------------------------------------")
            return f"{main_msg}\n" + "\n".join(err_card)

        if extra_lines:
            return f"{main_msg}\n" + "\n".join(extra_lines)

        return main_msg


class HumanFileFormatter(logging.Formatter):
    """Human-readable formatter for receiver.log; outputs boxes directly without prefixing box lines."""

    def __init__(self):
        super().__init__(
            fmt="%(asctime)s.%(msecs)03dZ [%(levelname)-7s] [%(name)s] [Thread:%(threadName)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if getattr(record, "is_box", False) or msg.startswith("┌"):
            return msg
        return super().format(record)


class EvaluationLogger:
    """Thread-safe JSONL logger dedicated to capturing evaluation metrics."""

    def __init__(self, log_path: str, session_id: Optional[str] = None):
        self.log_path = log_path
        self.session_id = (
            session_id
            or f"sess_{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        )
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(self.log_path)), exist_ok=True)

    def log_event(
        self,
        event: str,
        command_id: Optional[str] = None,
        command: Optional[str] = None,
        payload: Any = None,
        status: Optional[str] = None,
        duration_ms: Optional[float] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record an evaluation event as a JSON line in the evaluation file."""
        record: Dict[str, Any] = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "session_id": self.session_id,
            "event": event,
        }
        if command_id is not None:
            record["command_id"] = command_id
        if command is not None:
            record["command"] = command
        if payload is not None:
            record["payload"] = payload
        if status is not None:
            record["status"] = status
        if duration_ms is not None:
            record["duration_ms"] = round(duration_ms, 2)
        if details:
            record["details"] = details

        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as e:
                sys.stderr.write(f"[EVAL_LOGGER_ERROR] Failed to write event: {e}\n")

        return record

    def log_evaluation_record(
        self,
        command_id: str,
        command: str,
        device_status: str,
        tracker_status: str,
        failure_category: str,
        validation: str,
        expected_category: Optional[str] = None,
        recorded_reason: Optional[str] = None,
        expected_reason: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record a Member 4 validation entry in evaluation.jsonl."""
        record: Dict[str, Any] = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "command_id": command_id,
            "command": command,
            "device_status": device_status,
            "tracker_status": tracker_status,
            "failure_category": failure_category,
        }
        if expected_category and expected_category != failure_category:
            record["expected_category"] = expected_category
        if recorded_reason:
            record["recorded_reason"] = recorded_reason
        if expected_reason:
            record["expected_reason"] = expected_reason

        clean_val = "MATCH" if "MATCH" in validation and "MISMATCH" not in validation else "MISMATCH"
        record["validation"] = clean_val

        if details:
            record["details"] = details

        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as e:
                sys.stderr.write(f"[EVAL_LOGGER_ERROR] Failed to write validation entry: {e}\n")

        return record


def _load_logging_config(config_path: str) -> Dict[str, Any]:
    """Safely loads logging settings from the configuration file."""
    default_config = {
        "log_dir": "logs",
        "log_file": "receiver.log",
        "json_log_file": "receiver.jsonl",
        "eval_file": "evaluation.jsonl",
        "level": "INFO",
        "max_bytes": 10 * 1024 * 1024,  # 10 MB
        "backup_count": 5,
        "enable_eval_log": True,
    }
    base_dir = get_base_dir()
    candidates = []
    if config_path:
        candidates.append(config_path if os.path.isabs(config_path) else os.path.join(base_dir, config_path))
    candidates.append(os.path.join(base_dir, CONFIG_FILE_NAME))
    candidates.append(os.path.join(base_dir, "agents", CONFIG_FILE_NAME))

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "agents", CONFIG_FILE_NAME))
        candidates.append(os.path.join(sys._MEIPASS, CONFIG_FILE_NAME))

    for path in candidates:
        try:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    logging_cfg = data.get("logging", {})
                    default_config.update(logging_cfg)
                break
        except Exception as e:
            sys.stderr.write(f"[WARN] Failed to load logging config from {path}: {e}\n")

    return default_config


def setup_logging(config_path: str = "agents/config.json") -> logging.Logger:
    """Initializes and returns the root application logger."""
    global _INITIALIZED, _EVAL_LOGGER_INSTANCE

    with _INIT_LOCK:
        if _INITIALIZED:
            return logging.getLogger(_ROOT_LOGGER_NAME)

        cfg = _load_logging_config(config_path)
        base_dir = get_base_dir()

        target_log_dir = os.path.join(base_dir, cfg.get("log_dir", "logs"))
        try:
            os.makedirs(target_log_dir, exist_ok=True)
            log_dir = target_log_dir
        except OSError:
            fallback_root = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.path.expanduser("~")
            log_dir = os.path.join(fallback_root, "SynaptiMesh", "logs")
            os.makedirs(log_dir, exist_ok=True)
            sys.stderr.write(f"[LOG_INIT] Write permission denied in {target_log_dir}. Redirecting logs to {log_dir}\n")

        human_log_path = os.path.join(log_dir, cfg.get("log_file", "receiver.log"))
        json_log_path = os.path.join(log_dir, cfg.get("json_log_file", "receiver.jsonl"))
        eval_file_path = os.path.join(log_dir, cfg.get("eval_file", "evaluation.jsonl"))

        raw_level = cfg.get("level", "INFO").upper()
        level = getattr(logging, raw_level, logging.INFO)

        logger = logging.getLogger(_ROOT_LOGGER_NAME)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        if logger.hasHandlers():
            logger.handlers.clear()

        # 1. Clean Console Handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(CleanConsoleFormatter())
        logger.addHandler(console_handler)

        # 2. Structured JSON-Lines Rotating File Handler
        try:
            json_file_handler = RotatingFileHandler(
                json_log_path,
                maxBytes=int(cfg.get("max_bytes", 10 * 1024 * 1024)),
                backupCount=int(cfg.get("backup_count", 5)),
                encoding="utf-8",
            )
            json_file_handler.setLevel(logging.DEBUG)
            json_file_handler.setFormatter(JSONFormatter())
            logger.addHandler(json_file_handler)
        except Exception as e:
            sys.stderr.write(f"[ERROR] Could not create JSON log handler: {e}\n")

        # 3. Tabular Human-Readable Rotating File Handler
        try:
            human_file_handler = RotatingFileHandler(
                human_log_path,
                maxBytes=int(cfg.get("max_bytes", 10 * 1024 * 1024)),
                backupCount=int(cfg.get("backup_count", 5)),
                encoding="utf-8",
            )
            human_file_handler.setLevel(logging.DEBUG)
            human_file_handler.setFormatter(HumanFileFormatter())
            logger.addHandler(human_file_handler)
        except Exception as e:
            sys.stderr.write(f"[ERROR] Could not create human log handler: {e}\n")

        # 4. Root Directory Log Sync
        try:
            root_stderr_path = os.path.join(base_dir, "receiver.stderr.log")
            root_stderr_handler = RotatingFileHandler(
                root_stderr_path,
                maxBytes=5 * 1024 * 1024,
                backupCount=2,
                encoding="utf-8",
            )
            root_stderr_handler.setLevel(logging.WARNING)
            root_stderr_handler.setFormatter(JSONFormatter())
            logger.addHandler(root_stderr_handler)

            root_stdout_path = os.path.join(base_dir, "receiver.stdout.log")
            root_stdout_handler = RotatingFileHandler(
                root_stdout_path,
                maxBytes=5 * 1024 * 1024,
                backupCount=2,
                encoding="utf-8",
            )
            root_stdout_handler.setLevel(logging.INFO)
            root_stdout_handler.setFormatter(CleanConsoleFormatter())
            logger.addHandler(root_stdout_handler)
        except Exception as e:
            sys.stderr.write(f"[WARN] Could not attach root log files: {e}\n")

        # 5. Evaluation Logger
        if cfg.get("enable_eval_log", True):
            _EVAL_LOGGER_INSTANCE = EvaluationLogger(eval_file_path)
            _EVAL_LOGGER_INSTANCE.log_event(
                event="SYSTEM_INIT",
                details={
                    "pid": os.getpid(),
                    "python_version": sys.version,
                    "platform": sys.platform,
                    "log_dir": log_dir,
                },
            )

        _INITIALIZED = True
        return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Returns a named child logger under the 'receiver' namespace."""
    cli_bypass = any(arg in sys.argv for arg in ("-h", "--help", "-l", "--list-ports", "-v", "--version"))
    if not _INITIALIZED and not cli_bypass:
        setup_logging()

    if not name or name == _ROOT_LOGGER_NAME:
        return logging.getLogger(_ROOT_LOGGER_NAME)

    if not name.startswith(f"{_ROOT_LOGGER_NAME}."):
        name = f"{_ROOT_LOGGER_NAME}.{name}"

    return logging.getLogger(name)


def get_eval_logger() -> Optional[EvaluationLogger]:
    """Returns the active evaluation telemetry logger instance."""
    cli_bypass = any(arg in sys.argv for arg in ("-h", "--help", "-l", "--list-ports", "-v", "--version"))
    if not _INITIALIZED and not cli_bypass:
        setup_logging()
    return _EVAL_LOGGER_INSTANCE
