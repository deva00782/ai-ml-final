"""
SocketIO event handlers and Web logger for mobile device monitoring.
Consolidates socket event listeners and WebLogHandler into a single module.
"""

import time
import logging
import sys
from pathlib import Path

from flask_socketio import emit

try:
    from .config import DEBUG_MODE
except Exception:
    try:
        from config import DEBUG_MODE
    except Exception:
        DEBUG_MODE = True


class WebLogHandler(logging.Handler):
    """Custom log handler that emits log messages via SocketIO."""

    def __init__(self, socketio):
        super().__init__()
        self.socketio = socketio

    def emit(self, record):
        try:
            msg = record.getMessage()
            self.socketio.emit('log_message', {
                'message': msg,
                'level': record.levelname
            })
        except Exception:
            pass


def setup_logger(socketio):
    """
    Set up the logger with both web and console handlers.

    Args:
        socketio: Flask-SocketIO instance for real-time log emission

    Returns:
        logging.Logger: Configured logger instance
    """
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)

    # Silence noisy third-party libraries that spam DEBUG logs
    for noisy_logger in ("comtypes", "asyncio", "urllib3", "engineio", "socketio", "werkzeug"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    # Clear existing handlers to avoid duplicates
    logger.handlers.clear()

    # Web handler for real-time updates
    web_handler = WebLogHandler(socketio)
    web_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(web_handler)

    # Console handler for local debugging
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(console_handler)

    return logger


def register_socket_events(socketio, mqtt_service, pipeline_scheduler=None, action_observer=None):
    """
    Register all Flask-SocketIO event handlers for mobile devices.

    Args:
        socketio: Flask-SocketIO instance
        mqtt_service: MQTTService instance for publishing commands
        pipeline_scheduler: Optional legacy scheduler parameter (default: None)
        action_observer: Optional callback for UART action observation
    """

    @socketio.on('connect')
    def handle_connect():
        if pipeline_scheduler:
            emit('sync_state', {'is_paused': pipeline_scheduler.is_paused()})
        else:
            emit('sync_state', {'is_paused': True})

    @socketio.on('toggle_pause')
    def handle_pause(data):
        should_pause = data.get('paused', False)
        if pipeline_scheduler:
            pipeline_scheduler.toggle_pause(should_pause)

        # Toggle media subscription based on pause state
        mqtt_service.toggle_media_subscription(not should_pause)

    @socketio.on('manual_command')
    def handle_manual(data):
        raw_key = data.get('raw_command')
        conf = data.get('confidence', 0.95)
        ts = time.time()

        logging.info(f"[MANUAL TRIGGER] Dispatching {raw_key}")
        mqtt_service.publish_command(raw_key, conf, ts)
        if action_observer:
            action_observer(raw_key, "mobile")

        # Activate manual command mode to allow media updates
        mqtt_service.activate_manual_command_mode(duration_seconds=10)

    @socketio.on('live_search_input')
    def handle_search_input(data):
        query_text = data.get('query', '')
        if not query_text or not query_text.strip():
            return

        logging.info(f"[MANUAL SEARCH] Query: '{query_text.strip()}'")
        mqtt_service.publish_search_query(query_text.strip())
