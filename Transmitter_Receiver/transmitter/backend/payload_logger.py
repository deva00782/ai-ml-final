"""Payload logger for transmitted commands - stores what transmitter sends."""

import json
import logging
import os
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PAYLOAD_LOGGER = None
_INIT_LOCK = threading.Lock()
_LOG_FILE = Path(__file__).resolve().parents[2] / "logs" / "transmitted_payloads.jsonl"


def _get_payload_logger():
    global _PAYLOAD_LOGGER
    if _PAYLOAD_LOGGER is not None:
        return _PAYLOAD_LOGGER

    with _INIT_LOCK:
        if _PAYLOAD_LOGGER is not None:
            return _PAYLOAD_LOGGER

        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

        logger = logging.getLogger("transmitter.payloads")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        handler = RotatingFileHandler(
            _LOG_FILE,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

        _PAYLOAD_LOGGER = logger
        return logger


def log_mqtt_payload(topic: str, payload: dict, target: str = "mobile"):
    """Log an MQTT payload being sent and asynchronously mirror to Sync API if enabled."""
    logger = _get_payload_logger()
    record = {
        "type": "mqtt",
        "timestamp": datetime.now().isoformat(),
        "topic": topic,
        "target": target,
        "payload": payload,
    }
    logger.info(json.dumps(record, separators=(",", ":")))

    # Mirror to SynaptiMesh Sync API asynchronously if configured
    if os.environ.get("SYNC_API_ENABLED", "false").lower() in ("true", "1", "yes"):
        try:
            try:
                from mobile import sync_api_client as sync_client
            except ImportError:
                import sync_api_client as sync_client
            sync_client.send_event_async(
                kind="command_dispatch",
                device_id=payload.get("target_device", ""),
                status="DISPATCHED",
                command_id=payload.get("command_id", ""),
                topic=topic,
                payload=payload,
            )
        except Exception:
            pass


def log_uart_payload(command: str, payload_type: str = "action"):
    """Log a UART payload being sent."""
    logger = _get_payload_logger()
    record = {
        "type": "uart",
        "timestamp": datetime.now().isoformat(),
        "wire_command": command,
        "payload_type": payload_type,
    }
    logger.info(json.dumps(record, separators=(",", ":")))


def get_log_path() -> str:
    return str(_LOG_FILE)
