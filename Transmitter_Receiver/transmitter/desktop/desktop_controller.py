"""
Desktop Dashboard backend wiring.

Faithful re-wiring of the original `web_dash_board/JioSaavnController.py` so it
registers onto the SHARED master Flask app + SocketIO instance instead of
creating its own server. All JioSaavn control logic lives in the copied
`desktop_modules/` package (operate_jiosavaan, browser_manager, player_engine,
bci_pipeline, logger, os_operations) and is used UNCHANGED.

Socket.IO traffic is scoped to a namespace (default '/desktop') so it never
mixes with the mobile dashboard's events.
"""

import time

from flask import request, jsonify

try:
    from .logger import logger
    from .operate_jiosavaan import JioSaavnController
    from .os_operations import set_system_master_volume
except ImportError:
    from logger import logger
    from operate_jiosavaan import JioSaavnController
    from os_operations import set_system_master_volume


def _register_action_routes(app, controller, emit_to_desktop, action_observer):
    @app.route("/api/action", methods=["POST"])
    def handle_action():
        data = request.json or {}
        action = data.get("action")
        query = data.get("query")
        logger.info("WebServer", f"Received manual execution for: {action}")

        from bci_pipeline import DISPLAY_LABELS, COMMAND_MAPPING

        display_lbl = DISPLAY_LABELS.get(action, action)
        emit_to_desktop(
            "command_dispatched",
            {
                "command": action,
                "display_label": display_lbl,
                "action": COMMAND_MAPPING.get(action, action),
                "confidence": 0.95,
                "timestamp": time.time(),
                "domain": "AI_ML",
                "sample": "Manual",
            },
        )

        from backend.command_router import command_router
        route_res = command_router.route(action, target="desktop", query=query)
        result = route_res.get("result", route_res.get("reason", "Dispatched"))
        if action_observer and route_res.get("channel") != "uart":
            action_observer(action, "desktop")

        status = controller.get_status()
        emit_to_desktop("status_update", status)

        return jsonify(
            {
                "status": "success",
                "message": result,
                "volume": status.get("volume", 100),
                "is_playing": status.get("is_playing", False),
                "automation_active": status.get("automation_active", False),
            }
        )

    @app.route("/api/volume", methods=["POST"])
    def handle_direct_volume():
        data = request.json or {}
        vol_pct = data.get("volume")
        if vol_pct is not None:
            actual = set_system_master_volume(int(vol_pct))
            controller.current_volume = float(actual)
            status = controller.get_status()
            emit_to_desktop("status_update", status)
            return jsonify({"status": "success", "volume": actual})
        return jsonify({"status": "error", "message": "Missing volume parameter"}), 400

    @app.route("/api/config", methods=["GET", "POST"])
    def handle_config():
        if request.method == "GET":
            return jsonify({"status": "success", "config": controller.get_config()})

        data = request.json or {}
        confidence = data.get("confidence_threshold")
        cooldown = data.get("volume_cooldown")
        headless = data.get("headless")

        updated_config = controller.update_config(
            confidence_threshold=confidence,
            volume_cooldown=cooldown,
            headless=headless,
        )
        emit_to_desktop("config_update", updated_config)
        return jsonify({"status": "success", "config": updated_config})


def _register_core_routes(app, controller, emit_to_desktop, action_observer):
    @app.route("/api/automation", methods=["GET", "POST"])
    def handle_automation():
        if request.method == "GET":
            return jsonify(
                {
                    "status": "success",
                    "automation_active": controller.is_automation_active(),
                }
            )

        data = request.json or {}
        action = data.get("action", "toggle")
        if action == "start":
            controller.bci_processor.start_automation()
        elif action == "stop":
            controller.bci_processor.stop_automation()
        else:
            controller.toggle_automation()

        status = controller.get_status()
        emit_to_desktop("status_update", status)
        return jsonify(
            {
                "status": "success",
                "automation_active": controller.is_automation_active(),
            }
        )

    @app.route("/api/desktop/close", methods=["POST"])
    def handle_desktop_close():
        controller.close_browser()
        status = controller.get_status()
        emit_to_desktop("status_update", status)
        return jsonify({"status": "success", "message": "Chrome closed"})

    @app.route("/api/search", methods=["POST"])
    def handle_search():
        data = request.json or {}
        query = data.get("query")
        logger.info("WebServer", f"Received search request for: '{query}'")
        from backend.command_router import command_router
        route_res = command_router.route("Search Album/Playlist", target="desktop", query=query)
        result = route_res.get("result", route_res.get("reason", "Dispatched"))
        if action_observer and route_res.get("channel") != "uart":
            action_observer("Search Album/Playlist", "desktop")

        status = controller.get_status()
        emit_to_desktop("status_update", status)

        return jsonify(
            {"status": "success", "message": result, "volume": status.get("volume", 100)}
        )


def _register_socket_handlers(socketio, namespace, controller, emit_to_desktop, action_observer):
    @socketio.on("toggle_automation", namespace=namespace)
    def handle_toggle_automation_socket(data=None):
        action = data.get("action", "toggle") if data else "toggle"
        if action == "start":
            controller.bci_processor.start_automation()
        elif action == "stop":
            controller.bci_processor.stop_automation()
        else:
            controller.toggle_automation()

        status = controller.get_status()
        emit_to_desktop("status_update", status)

    @socketio.on("manual_command", namespace=namespace)
    def handle_manual_socket(data):
        raw_cmd = data.get("raw_command")
        conf = data.get("confidence", 0.95)
        if raw_cmd:
            logger.info("SocketIO", f"Received manual command: '{raw_cmd}'")
            from bci_pipeline import DISPLAY_LABELS, COMMAND_MAPPING

            display_lbl = DISPLAY_LABELS.get(raw_cmd, raw_cmd)
            emit_to_desktop(
                "command_dispatched",
                {
                    "command": raw_cmd,
                    "display_label": display_lbl,
                    "action": COMMAND_MAPPING.get(raw_cmd, raw_cmd),
                    "confidence": conf,
                    "timestamp": time.time(),
                    "domain": "AI_ML",
                    "sample": "Manual",
                },
            )
            from backend.command_router import command_router
            result = command_router.route(raw_cmd, target="desktop")
            if action_observer and (not isinstance(result, dict) or result.get("channel") != "uart"):
                action_observer(raw_cmd, "desktop")
            status = controller.get_status()
            emit_to_desktop("status_update", status)


def register_desktop_backend(app, socketio, namespace="/desktop", action_observer=None, unified_mode=False):
    """Register the desktop dashboard routes + socket handlers on `app`.

    Args:
        unified_mode: When True, skips registering /api/action, /api/config, and /api/volume
                      so the unified hub handlers in app.py take precedence without collision.

    Returns:
        (controller, status_poller) where `status_poller` is a callable for
        the caller to run as a SocketIO background task.
    """
    controller = JioSaavnController()

    def emit_to_desktop(event_name, data=None):
        socketio.emit(event_name, data, namespace=namespace)

    # Route real-time logs and BCI events only to desktop clients.
    logger.set_emitter(lambda msg: emit_to_desktop("log_update", {"message": msg}))
    controller.bci_processor.set_socket_emitter(emit_to_desktop)

    if not unified_mode:
        _register_action_routes(app, controller, emit_to_desktop, action_observer)
    _register_core_routes(app, controller, emit_to_desktop, action_observer)
    _register_socket_handlers(socketio, namespace, controller, emit_to_desktop, action_observer)

    def status_poller():
        while True:
            try:
                status = controller.get_status()
                socketio.emit("status_update", status, namespace=namespace)
            except Exception:
                pass
            socketio.sleep(1)

    return controller, status_poller