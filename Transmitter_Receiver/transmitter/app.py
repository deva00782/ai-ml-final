"""
SynaptiMesh — Unified Dashboard (Experimental)
===============================================
Single route /dashboard that controls both Mobile (MQTT) and Desktop (Selenium)
through one FSM pipeline. Execution stays platform-specific, UI is common.

Architecture:
  BCI / Test -> receive_cortex_command -> 4s window -> FSM (single source) -> Mapper -> Router -> Mobile/Desktop

Do NOT modify legacy mappings. Use 4s window, 0.35 threshold, 2s debounce as-is.
BCI buttons use ONE path: POST /api/bci/command -> receive_cortex_command
"""
import atexit
import os, sys, threading, logging, warnings, time, subprocess
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
warnings.filterwarnings("ignore", category=DeprecationWarning)

from flask import Flask, render_template, send_from_directory, request, jsonify, redirect
from flask_socketio import SocketIO
from jinja2 import ChoiceLoader, FileSystemLoader
try:
    from backend.uart_transmitter import uart_transmitter
except ImportError:
    from uart_transmitter import uart_transmitter
from command_system.command_mapper import canonical_action_for
try:
    from backend import boxed_logger
except ImportError:
    import boxed_logger
boxed_logger.setup_terminal_logger()

# PostgreSQL integration
try:
    from backend.postgres_store import init_postgres, close_postgres, get_postgres_store
except ImportError:
    init_postgres = close_postgres = get_postgres_store = None


def _observe_uart_action(command, device=None):
    """Observe existing direct-device commands without changing their dispatch."""
    # Mobile/APK commands go via MQTT only: PUSH selects the mobile (APK)
    # receiver, and in that mode the UART receiver (controlling the other
    # desktop) must NOT fire.
    if device == "mobile":
        return
    if not (uart_transmitter and uart_transmitter.enabled and uart_transmitter.is_connected()):
        return
    action = canonical_action_for(command, device) if device else command
    if action:
        uart_transmitter.send_action(action)


atexit.register(uart_transmitter.shutdown)

# --- Receiver-style boxed logging helpers (trace id continuity) ---
_cmd_trace = {"id": None, "cmd": None, "ts": 0.0}


def _trace_cmd_id(raw):
    """Return a stable cmd_* id per logical command, receiver style."""
    key = str(raw or "").strip().upper()
    if not key:
        return None
    now = time.time()
    if " " in key:  # combination (e.g. "PUSH RIGHT") -> reuse the last received id
        if _cmd_trace["id"] and (now - _cmd_trace["ts"]) < 15:
            _cmd_trace["cmd"] = key
            return _cmd_trace["id"]
    if _cmd_trace["cmd"] == key and (now - _cmd_trace["ts"]) < 10:
        return _cmd_trace["id"]
    _cmd_trace.update({"id": boxed_logger.new_cmd_id(), "cmd": key, "ts": now})
    return _cmd_trace["id"]

BASE_DIR = Path(__file__).resolve().parent
MOBILE_DIR = BASE_DIR / "mobile"
DESKTOP_DIR = BASE_DIR / "desktop"
FRONTEND_DIR = BASE_DIR / "frontend"
FRONTEND_TEMPLATE_DIR = FRONTEND_DIR / "templates"
FRONTEND_STATIC_DIR = FRONTEND_DIR / "static"
for p in [str(DESKTOP_DIR), str(MOBILE_DIR)]:
    if p not in sys.path: sys.path.insert(0, p)

MOBILE_NAMESPACE = "/mobile"
DESKTOP_NAMESPACE = "/desktop"
DASHBOARD_NAMESPACE = "/dashboard"

class NamespacedSocketIO:
    def __init__(self, socketio, namespace):
        self._socketio=socketio; self._namespace=namespace
    def emit(self, event, *args, **kwargs):
        p_kwargs = dict(kwargs)
        p_kwargs["namespace"] = self._namespace
        res = self._socketio.emit(event, *args, **p_kwargs)
        # Bridge device lifecycle events directly to /dashboard for instant (<50ms) discovery
        if self._namespace != DASHBOARD_NAMESPACE and event in (
            "mobile_device_registered", "mobile_devices_updated",
            "active_mobile_device_changed", "mobile_devices_cleared", "device_status_changed"
        ):
            d_kwargs = dict(kwargs)
            d_kwargs["namespace"] = DASHBOARD_NAMESPACE
            try:
                self._socketio.emit(event, *args, **d_kwargs)
            except Exception:
                pass
        return res
    def on(self, event, **kwargs):
        kwargs.setdefault("namespace", self._namespace)
        return self._socketio.on(event,**kwargs)

app = Flask(__name__,
    template_folder=str(FRONTEND_TEMPLATE_DIR),
    static_folder=str(FRONTEND_STATIC_DIR))
app.config["SECRET_KEY"]=os.environ.get("SECRET_KEY","unified_dashboard_secret")
app.config["TEMPLATES_AUTO_RELOAD"]=True
app.config["SEND_FILE_MAX_AGE_DEFAULT"]=0

# Security Review (CSRF - python:S4502): Loopback controller dashboard with local REST and SocketIO
try:
    from flask_wtf.csrf import CSRFProtect
    csrf = CSRFProtect()
    csrf.init_app(app)
    app.config["WTF_CSRF_CHECK_DEFAULT"] = False
except Exception:
    pass

@app.after_request
def _no_cache(r):
    r.headers["Cache-Control"]="no-store, no-cache, must-revalidate, max-age=0"
    r.headers["Pragma"]="no-cache"
    return r

# Jinja loader resolves all templates (dashboard.html, mobile.html, etc.) from frontend/templates
app.jinja_loader = FileSystemLoader(str(FRONTEND_TEMPLATE_DIR))

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# --- Mobile backend ---
try:
    from mobile.config import DEBUG_MODE
except Exception:
    try:
        from config import DEBUG_MODE
    except Exception:
        DEBUG_MODE = False
try:
    from mobile.socket_events import setup_logger, register_socket_events
    from mobile.mqtt_service import MQTTService
    mobile_sio = NamespacedSocketIO(socketio, MOBILE_NAMESPACE)
    setup_logger(mobile_sio)
    mqtt_service = MQTTService(mobile_sio, pipeline_scheduler=None)
    pipeline_scheduler = None
    register_socket_events(mobile_sio, mqtt_service, None, action_observer=_observe_uart_action)
    threading.Thread(target=mqtt_service.connect, daemon=True, name="MQTTService-Connect").start()
    MOBILE_AVAILABLE = True
except Exception as e:
    logging.getLogger().warning(f"[UNIFIED] Mobile backend not available: {e}")
    mqtt_service = None; pipeline_scheduler = None; MOBILE_AVAILABLE = False

# --- Desktop backend (lazy init) ---
_desktop_initialized = False
desktop_controller = None
desktop_status_poller = lambda: None
DESKTOP_AVAILABLE = False

def _init_desktop_backend():
    global desktop_controller, desktop_status_poller, DESKTOP_AVAILABLE, _desktop_initialized
    if _desktop_initialized:
        return
    _desktop_initialized = True
    try:
        try:
            from desktop.desktop_controller import register_desktop_backend
        except ImportError:
            from desktop_controller import register_desktop_backend
        desktop_controller, desktop_status_poller = register_desktop_backend(
            app, socketio, namespace=DESKTOP_NAMESPACE, action_observer=_observe_uart_action, unified_mode=True
        )
        DESKTOP_AVAILABLE = True
    except Exception as e:
        logging.getLogger().warning(f"[UNIFIED] Desktop backend not available: {e}")
        desktop_controller = None
        desktop_status_poller = lambda: None
        DESKTOP_AVAILABLE = False

# --- Backend registry (init desktop first) ---
_init_desktop_backend()
from backend.device_manager import device_manager, state_manager
from backend.command_router import command_router
if MOBILE_AVAILABLE and mqtt_service:
    device_manager.register_mobile(mqtt_service, pipeline_scheduler)
    state_manager.register_device("mobile")
if DESKTOP_AVAILABLE and desktop_controller:
    device_manager.register_desktop(desktop_controller)
    state_manager.register_device("desktop")

# --- FSM layer (single source of truth) ---
from command_system.command_mapper import CommandMapper
from command_system.fsm_controller import FSMController
from command_system.cortex_bridge import create_cortex_bridge

dashboard_sio = NamespacedSocketIO(socketio, DASHBOARD_NAMESPACE)
fsm_mapper = CommandMapper(command_router=command_router, dry_run=False)
fsm_controller = FSMController(mapper=fsm_mapper, dry_run=False, combo_timeout=4.0)

# (BCI Recording & Replay removed for production)

# --- JioSaavn auto-launch state (secondary, not dominant) ---
from command_system import (
    OPEN_MOBILE_DASHBOARD,
    OPEN_DESKTOP_DASHBOARD,
    BACK_TO_LEVEL_2,
    RETURN_TO_LEVEL_2,
    RETURN_TO_LEVEL_1,
    BACK_TO_MAIN_DASHBOARD,
)
jiosaavn_status = {
    "mobile": {"status": "idle", "message": "Idle"},
    "desktop": {"status": "idle", "message": "Idle"},
    "overall": "idle"
}
_last_jiosaavn_launch = {"device": None, "ts": 0.0}
_jiosaavn_lock = threading.Lock()
# True while the Mobile (APK) dashboard is the active Level 3 view, so leaving
# it can close JioSaavn on the phone.
_mobile_dashboard_open = False

def _set_jiosaavn_status(device, status, message):
    with _jiosaavn_lock:
        jiosaavn_status[device]["status"] = status
        jiosaavn_status[device]["message"] = message
        jiosaavn_status["overall"] = status
    socketio.emit("jiosaavn_status", {"device": device, "status": status, "message": message, "jiosaavn": dict(jiosaavn_status)}, namespace=DASHBOARD_NAMESPACE)
    logging.getLogger().info(f"[JIOSAAVN] {device} -> {status}: {message}")

def _close_mobile_jiosaavn(reason="back to level"):
    global _mobile_dashboard_open
    if not _mobile_dashboard_open:
        return
    _mobile_dashboard_open = False
    try:
        command_router.route("Right_Return_to_Home", target="mobile")
        _set_jiosaavn_status("mobile", "closed", "Closed (back to level)")
        logging.getLogger().info(f"[JIOSAAVN] Mobile close requested ({reason})")
    except Exception:
        logging.getLogger().exception("[JIOSAAVN] Failed to close Mobile JioSaavn")

def _close_desktop_jiosaavn(reason="back to level"):
    try:
        uart_active = bool(uart_transmitter and uart_transmitter.enabled and uart_transmitter.is_connected())
    except Exception:
        uart_active = False

    if uart_active:
        logging.getLogger().info(f"[JIOSAAVN] Forwarding close to secondary device (UART) ({reason})")
        _set_jiosaavn_status("desktop", "closed", f"Closed ({reason})")
        uart_transmitter.send_action("back_to_level_2")
    else:
        try:
            if DESKTOP_AVAILABLE and desktop_controller is not None:
                desktop_controller.close_browser()
            _set_jiosaavn_status("desktop", "closed", "Closed (back to level)")
            logging.getLogger().info(f"[JIOSAAVN] Desktop Chrome closed ({reason})")
        except Exception:
            logging.getLogger().exception("[JIOSAAVN] Failed to close Desktop Chrome")

def _launch_jiosaavn_async(device):
    """Reuse existing launch mechanisms. Never crashes FSM."""
    try:
        _set_jiosaavn_status(device, "opening", "Opening..." if device=="desktop" else "Launching...")
        _print_jiosaavn_init_box(device)
        if device == "mobile":
            _launch_mobile_jiosaavn()
        else:
            _launch_desktop_jiosaavn()
    except Exception:
        logging.getLogger().exception(f"[JIOSAAVN] Failed to launch {device} JioSaavn")
        _set_jiosaavn_status(device, "failed", "Launch error")


def _print_jiosaavn_init_box(device):
    boxed_logger.print_box("JIOSAAVN INITIALIZATION", [
        ("Device", device),
        ("Action", "Right_jiosaavn" if device == "mobile" else "open_desktop_dashboard"),
        ("Time", boxed_logger.now_str()),
        ("Status", "OPENING"),
    ])


def _launch_mobile_jiosaavn():
    logging.getLogger().info("[FSM] Entered MOBILE_DASHBOARD_ACTIVE")
    logging.getLogger().info("[JIOSAAVN] Launching Mobile JioSaavn")
    # Existing Android mechanism: publish Right_jiosaavn via MQTT router
    result = command_router.route("Right_jiosaavn", target="mobile")
    dispatched = result.get("dispatched", False)
    if dispatched:
        _set_jiosaavn_status("mobile", "ready", "Launch requested")
        logging.getLogger().info("[JIOSAAVN] Mobile JioSaavn launch requested")
    else:
        _set_jiosaavn_status("mobile", "failed", result.get("reason") or "Launch failed")
        logging.getLogger().warning(f"[JIOSAAVN] Failed to launch Mobile JioSaavn: {result}")


def _launch_desktop_jiosaavn():
    logging.getLogger().info("[FSM] Entered DESKTOP_DASHBOARD_ACTIVE")
    try:
        if uart_transmitter and uart_transmitter.enabled and not uart_transmitter.is_connected():
            uart_transmitter.connect()
        uart_active = bool(uart_transmitter and uart_transmitter.enabled and uart_transmitter.is_connected())
    except Exception:
        uart_active = False

    if uart_active:
        logging.getLogger().info("[JIOSAAVN] Desktop JioSaavn open forwarded to secondary device (UART)")
        _set_jiosaavn_status("desktop", "ready", "Forwarded via UART")
        uart_transmitter.send_action("open_desktop_dashboard")
    else:
        logging.getLogger().info("[JIOSAAVN] UART disconnected: Opening JioSaavn locally via desktop_controller")
        if desktop_controller:
            try:
                desktop_controller.browser_manager.focus_or_open_tab("https://www.jiosaavn.com/")
                _set_jiosaavn_status("desktop", "ready", "Launched locally")
            except Exception as e:
                _set_jiosaavn_status("desktop", "failed", f"Local launch error: {e}")

def _maybe_auto_launch(result):
    """Trigger only on actual dashboard activation, with duplicate guard."""
    global _mobile_dashboard_open
    action = result.get("action")
    if action not in (OPEN_MOBILE_DASHBOARD, OPEN_DESKTOP_DASHBOARD):
        return
    device = "mobile" if action == OPEN_MOBILE_DASHBOARD else "desktop"
    if device == "mobile":
        _mobile_dashboard_open = True
    now = time.time()
    # Duplicate guard: dedup already suppresses repeated pushes, but add 3s cooldown per device
    with _jiosaavn_lock:
        if _last_jiosaavn_launch["device"] == device and (now - _last_jiosaavn_launch["ts"]) < 3.0:
            logging.getLogger().info(f"[JIOSAAVN] Duplicate launch suppressed for {device}")
            return
        _last_jiosaavn_launch["device"] = device
        _last_jiosaavn_launch["ts"] = now
    threading.Thread(target=_launch_jiosaavn_async, args=(device,), daemon=True).start()

def _get_current_volume():
    """Return actual known volume. Desktop system volume is source of truth; fallback 50. Mobile volume via MQTT media_update not reliably absolute, so desktop is used."""
    try:
        if DESKTOP_AVAILABLE and desktop_controller is not None:
            st = desktop_controller.get_status()
            v = st.get("volume")
            if v is not None:
                return int(max(0, min(100, int(float(v)))))
    except Exception:
        pass
    return 50

_volume_state = {"volume": _get_current_volume(), "source": "desktop" if DESKTOP_AVAILABLE else "fallback"}

def _emit_volume(force=False):
    vol = _get_current_volume()
    if force or vol != _volume_state["volume"]:
        _volume_state["volume"] = vol
        socketio.emit("volume_update", {"volume": vol, "source": _volume_state["source"]}, namespace=DASHBOARD_NAMESPACE)
        logging.getLogger().info(f"[VOLUME] {vol}% (source={_volume_state['source']})")

def _volume_poller():
    """Poll desktop volume every 1s and emit to dashboard if changed."""
    last = None
    while True:
        try:
            vol = _get_current_volume()
            if vol != last:
                last = vol
                _volume_state["volume"] = vol
                socketio.emit("volume_update", {"volume": vol, "source": "desktop"}, namespace=DASHBOARD_NAMESPACE)
        except Exception:
            pass
        socketio.sleep(1.0)

def _emit_state():
    socketio.emit("fsm_state_update", fsm_controller.get_state(), namespace=DASHBOARD_NAMESPACE)
    # also push jiosaavn status so header stays in sync
    socketio.emit("jiosaavn_status", {"jiosaavn": dict(jiosaavn_status)}, namespace=DASHBOARD_NAMESPACE)
    # also push window duration + volume so config UI stays in sync
    try:
        socketio.emit("config_update", {"command_window_seconds": fsm_controller.get_window_duration(), "volume": _volume_state["volume"]}, namespace=DASHBOARD_NAMESPACE)
    except Exception:
        pass

def _fsm_result_with_state(result):
    # enrich with target_device for UI, ensure level present
    result["target_device"] = fsm_controller.target_device
    socketio.emit("fsm_command_result", result, namespace=DASHBOARD_NAMESPACE)
    _emit_state()
    _maybe_auto_launch(result)
    # After volume command, push updated volume (system volume changed via PlayerEngine)
    if result.get("action") in ("volume_up", "volume_down"):
        # give OS a moment to settle (especially macOS osascript)
        def _delayed_vol():
            socketio.sleep(0.3)
            _emit_volume(force=True)
        socketio.start_background_task(_delayed_vol)
    action = result.get("action")
    # Leaving the Mobile (APK) or Desktop dashboard closes JioSaavn / Chrome
    if action in (BACK_TO_LEVEL_2, RETURN_TO_LEVEL_2, RETURN_TO_LEVEL_1, BACK_TO_MAIN_DASHBOARD):
        _close_mobile_jiosaavn(reason=action)
        _close_desktop_jiosaavn(reason=action)
    # Actions are already dispatched by FSMController via CommandMapper -> CommandRouter.
    # Do NOT re-dispatch here to prevent duplicate executions (e.g. double Next Track or double Play/Pause).
    _emit_execution_box(result)
    # state already emitted (including jiosaavn)

def _emit_execution_box(result):
    """COMMAND EXECUTION box, receiver style, with the current dispatch state."""
    action = result.get("action")
    if action in (None, "none", "wait_for_combination"):
        status = "PENDING"
    elif not result.get("dispatch"):
        status = "NO DISPATCH"
    else:
        dispatch = result.get("dispatch") or {}
        status = "BLOCKED" if dispatch.get("rejected") else "DISPATCHED"
    cmd_id = _trace_cmd_id(result.get("raw_command") or result.get("command") or action)
    raw = result.get("raw_command") or result.get("command") or str(action or "").upper()
    boxed_logger.print_box("COMMAND EXECUTION", [
        ("Command ID", cmd_id),
        ("Command", raw),
        ("Action", action),
        ("State", result.get("new_state")),
        ("Target", result.get("target_device")),
        ("Time", boxed_logger.now_str()),
        ("Status", status),
    ])

fsm_controller.set_result_callback(_fsm_result_with_state)

# --- Cortex bridge (optional, uses FSM's internal 4s window) ---
CORTEX_CLIENT_ID = os.environ.get("CORTEX_CLIENT_ID", "")
CORTEX_CLIENT_SECRET = os.environ.get("CORTEX_CLIENT_SECRET", "")
CORTEX_ENABLED = bool(CORTEX_CLIENT_ID and CORTEX_CLIENT_SECRET)
cortex_bridge=None
if CORTEX_ENABLED:
    def _cortex_cb(command, power, timestamp=None):
        logging.getLogger().info(f"[CORTEX] -> FSM: {command} power={power:.3f} ts={timestamp}")

        fsm_controller.receive_cortex_command(command, power, timestamp=timestamp)
    cortex_bridge=create_cortex_bridge(CORTEX_CLIENT_ID, CORTEX_CLIENT_SECRET, _cortex_cb, logging.getLogger())
    logging.getLogger().info("[UNIFIED] Cortex bridge initialized (FSM owns 4s window)")
else:
    logging.getLogger().warning("[UNIFIED] Cortex disabled (no credentials) - using test buttons")

# --- Socket handlers for unified dashboard (server->client only; no bci_command emit to avoid duplicate) ---
@socketio.on("connect", namespace=DASHBOARD_NAMESPACE)
def handle_dashboard_connect():
    # push current state on connect
    socketio.emit("fsm_state_update", fsm_controller.get_state(), namespace=DASHBOARD_NAMESPACE)
    socketio.emit("jiosaavn_status", {"jiosaavn": dict(jiosaavn_status)}, namespace=DASHBOARD_NAMESPACE)
    try:
        socketio.emit("volume_update", {"volume": _get_current_volume(), "source": _volume_state.get("source","desktop")}, namespace=DASHBOARD_NAMESPACE)
        socketio.emit("config_update", {"command_window_seconds": fsm_controller.get_window_duration()}, namespace=DASHBOARD_NAMESPACE)
    except Exception:
        pass

# --- REST API ---

@app.route("/", methods=["GET"])
def root():
    return redirect("/dashboard")

@app.route("/dashboard", methods=["GET"])
def dashboard():
    global _mobile_dashboard_open
    state_manager.set_active_dashboard("dashboard")
    # Start at the Sub-Master Hub (Level 2): reset FSM + JioSaavn status on open
    # so a leftover state (e.g. DESKTOP_DASHBOARD_ACTIVE) is not shown to the user.
    fsm_controller.reset()
    _mobile_dashboard_open = False
    with _jiosaavn_lock:
        jiosaavn_status["mobile"] = {"status": "idle", "message": "Idle"}
        jiosaavn_status["desktop"] = {"status": "idle", "message": "Idle"}
        jiosaavn_status["overall"] = "idle"
        _last_jiosaavn_launch["device"] = None
        _last_jiosaavn_launch["ts"] = 0.0
    return render_template("dashboard.html")

@app.route("/api/fsm/state", methods=["GET"])
def fsm_state():
    return jsonify({"status":"success","fsm":fsm_controller.get_state(), "jiosaavn": jiosaavn_status})

@app.route("/api/jiosaavn/status", methods=["GET"])
def jiosaavn_status_api():
    return jsonify({"status":"success","jiosaavn": jiosaavn_status})

@app.route("/api/fsm/reset", methods=["POST"])
def fsm_reset():
    global _mobile_dashboard_open
    fsm_controller.reset()
    _mobile_dashboard_open = False
    _close_desktop_jiosaavn(reason="fsm reset")
    with _jiosaavn_lock:
        jiosaavn_status["mobile"] = {"status": "idle", "message": "Idle"}
        jiosaavn_status["desktop"] = {"status": "idle", "message": "Idle"}
        jiosaavn_status["overall"] = "idle"
        _last_jiosaavn_launch["device"] = None
        _last_jiosaavn_launch["ts"] = 0.0
    socketio.emit("jiosaavn_status", {"jiosaavn": dict(jiosaavn_status)}, namespace=DASHBOARD_NAMESPACE)
    _emit_state()
    return jsonify({"status":"success","fsm":fsm_controller.get_state(), "jiosaavn": jiosaavn_status})

@app.route("/api/fsm/command", methods=["POST"])
def fsm_direct():
    """Direct process_command (no window) - for debugging only. BCI path uses /api/bci/command."""
    data=request.json or {}
    raw=data.get("command") or data.get("raw_command")
    conf=float(data.get("confidence",1.0))
    if not raw: return jsonify({"status":"error","message":"Missing command"}),400
    result=fsm_controller.process_command(raw, confidence=conf)
    _emit_state()
    socketio.emit("fsm_command_result", result, namespace=DASHBOARD_NAMESPACE)
    return jsonify({"status":"success","result":result,"fsm":fsm_controller.get_state()})

@app.route("/api/bci/command", methods=["POST"])
def bci_command():
    """Single BCI input path: receive_cortex_command -> 4s window -> FSM -> mapper -> router.
    For ordered doubles, frontend does two sequential POSTs (RIGHT then PUSH etc.)
    """
    data=request.json or {}
    raw=data.get("command") or data.get("raw_command")
    conf=float(data.get("confidence",0.95))
    if not raw: return jsonify({"status":"error","message":"Missing command"}),400

    accepted=fsm_controller.receive_cortex_command(raw, conf)
    cmd_id = _trace_cmd_id(raw)
    boxed_logger.print_box("COMMAND RECEIVED", [
        ("Command ID", cmd_id),
        ("Command", raw.upper()),
        ("Source", "HTTP"),
        ("Window", f"{fsm_controller.get_window_duration()}s"),
        ("Time", boxed_logger.now_str()),
        ("Status", "RECEIVED"),
    ])
    # result will be emitted via callback after window (single=after 4s, double=immediate if different)
    return jsonify({"status":"success","accepted":accepted,"command":raw,"confidence":conf,"fsm":fsm_controller.get_state()})

@app.route("/api/fsm/back", methods=["POST"])
def fsm_back():
    data=request.json or {}
    lvl=data.get("level")
    # Map unified back buttons: back L2 or L1
    # Use FSM navigate logic
    result=fsm_controller.navigate_back()
    # Leaving the mobile or desktop dashboard closes JioSaavn
    _close_mobile_jiosaavn(reason="back button")
    _close_desktop_jiosaavn(reason="back button")
    # If specific level requested and not matching, adjust
    if lvl==2 and fsm_controller.current_state!="SUB_MASTER_DASHBOARD":
        # PUSH+RIGHT path would be via window normally; here direct back to Level 2
        from command_system import SUB_MASTER_DASHBOARD
        fsm_controller.target_device=None; fsm_controller.dashboard_open=False
        fsm_controller.current_state=SUB_MASTER_DASHBOARD
        result={"action":"back_to_level_2","new_state":SUB_MASTER_DASHBOARD,"message":"Returned to Level 2 (Unified back)"}
    _emit_state()
    return jsonify({"status":"success","result":result,"fsm":fsm_controller.get_state()})
ERR_MOBILE_UNAVAILABLE = "Mobile backend not available"

_MANUAL_MOBILE_MAP = {
    "Play / Pause": "Right_Play_Pause", "Next Track": "Right_Next_Song",
    "Previous Track": "Right_Previous_Song", "Volume Up": "Right_Volume_Up",
    "Volume Down": "Right_Volume_Down", "Search Album/Playlist": "Right_Search_Playlist",
    "Search": "Right_Search_Playlist", "LEFT": "Right_Play_Pause",
    "PUSH": "Right_Next_Song", "PULL": "Right_Previous_Song",
    "RIGHT+PUSH": "Right_Volume_Up", "RIGHT+PULL": "Right_Volume_Down",
    "RIGHT": "Right_Search_Playlist", "PUSH+RIGHT": "Right_Close_App",
    "Back to Level 2": "Right_Close_App", "back_to_level_2": "Right_Close_App",
    "Right_Close_App": "Right_Close_App", "Close_App": "Right_Close_App",
    "Close JioSaavn": "Right_Close_App"
}


def _dispatch_mobile_manual_action(action, query, target):
    from backend.command_router import command_router as cr
    cmd = _MANUAL_MOBILE_MAP.get(action, action)
    if cmd in ("SEARCH", "Right_Search_Playlist"):
        effective_query = (query or "").strip() or "Inaam"
        return cr.route("SEARCH", target=target, query=effective_query)
    return cr.route(cmd, target=target, query=query)


def _dispatch_desktop_manual_action(action, query):
    from backend.command_router import command_router as cr
    is_search_act = action in ("SEARCH", "Right_Search_Playlist", "Search Album/Playlist", "Search", "RIGHT")
    fallback_query = "Inaam" if is_search_act else query
    effective_query = (query or "").strip() or fallback_query
    route_res = cr.route(action, target="desktop", query=effective_query)
    result = route_res.get("result", route_res.get("reason", "Dispatched"))
    if desktop_controller:
        status = desktop_controller.get_status()
        socketio.emit("status_update", status, namespace=DESKTOP_NAMESPACE)
    return result


@app.route("/api/action", methods=["POST"])
def handle_manual_action():
    """Manual fallback (mirrors desktop /api/action) - direct dispatch without FSM window."""
    data = request.json or {}
    action = data.get("action")
    query = data.get("query")
    target = data.get("target") or fsm_controller.target_device or "desktop"
    if target not in ("mobile", "desktop", "ios"):
        target = "mobile" if target == "ios" else "desktop"

    if target in ("mobile", "ios") and MOBILE_AVAILABLE:
        result = _dispatch_mobile_manual_action(action, query, target)
    elif desktop_controller:
        result = _dispatch_desktop_manual_action(action, query)
    else:
        result = "No backend available"
    return jsonify({"status": "success", "message": str(result), "target": target})


def _handle_mobile_target_search(query, target):
    from backend.command_router import command_router as cr
    result = cr.route("SEARCH", target=target, query=query)
    song_url = result.get("song_url", "") if isinstance(result, dict) else ""
    song_title = result.get("title", query) if isinstance(result, dict) else query
    return jsonify({"status": "success", "result": result, "query": query, "title": song_title, "song_url": song_url, "target": target})


@app.route("/api/mobile/search", methods=["POST"])
def handle_mobile_search():
    """Sprint 12 & 14: Dedicated SEARCH endpoint for JioSaavn with dynamic query (default: Inaam)."""
    data = request.json or {}
    query = data.get("query")
    target = data.get("target") or fsm_controller.target_device or "desktop"
    if target not in ("mobile", "desktop", "ios"):
        target = "desktop"
    query = (query or "").strip() or "Inaam"

    if target in ("mobile", "ios"):
        if MOBILE_AVAILABLE:
            return _handle_mobile_target_search(query, target)
        return jsonify({"status": "error", "message": ERR_MOBILE_UNAVAILABLE}), 400

    from backend.command_router import command_router as cr
    result = cr.route("SEARCH", target="desktop", query=query)
    if DESKTOP_AVAILABLE and desktop_controller:
        try:
            status = desktop_controller.get_status()
            socketio.emit("status_update", status, namespace=DESKTOP_NAMESPACE)
        except Exception:
            pass
    return jsonify({"status": "success", "result": result, "query": query, "title": query, "target": "desktop"})


@app.route("/api/mobile/devices", methods=["GET"])
def handle_mobile_devices():
    """Sprint 12: Returns all discovered mobile devices and the current active target."""
    if not mqtt_service:
        return jsonify({"active_device": "none", "devices": []})
    try:
        return jsonify(mqtt_service.get_registered_devices())
    except Exception:
        return jsonify({"active_device": getattr(mqtt_service, "active_device", "none"), "devices": []})


@app.route("/api/mobile/select_device", methods=["POST"])
def handle_select_mobile_device():
    """Sprint 12: Switches the active mobile device targeted by MQTT commands."""
    if not mqtt_service:
        return jsonify({"status": "error", "message": ERR_MOBILE_UNAVAILABLE}), 503
    data = request.json or {}
    username = data.get("username", "").strip()
    if not username:
        return jsonify({"status": "error", "message": "Missing username"}), 400
    try:
        mqtt_service.set_active_device(username)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    socketio.emit("active_mobile_device_changed", {"active_device": mqtt_service.active_device}, namespace=DASHBOARD_NAMESPACE)
    return jsonify({"status": "success", "active_device": mqtt_service.active_device})


@app.route("/api/mobile/delete_device", methods=["POST"])
def handle_delete_mobile_device():
    """Deletes an individual device by username or device_id."""
    data = request.json or {}
    identifier = data.get("username") or data.get("device_id") or ""
    if not identifier:
        return jsonify({"status": "error", "message": "Missing username or device_id"}), 400
    res = {}
    if mqtt_service:
        try:
            res = mqtt_service.delete_registered_device(identifier)
        except Exception as e:
            logging.getLogger().warning(f"[DELETE] MQTT delete failed: {e}")
    if get_postgres_store:
        try:
            pg = get_postgres_store()
            if pg:
                pg.delete_device(username=identifier)
        except Exception as e:
            logging.getLogger().warning(f"[DELETE] PostgreSQL delete failed: {e}")
    socketio.emit("mobile_devices_updated", res or {"devices": []}, namespace=DASHBOARD_NAMESPACE)
    return jsonify({"status": "success", "result": res})


@app.route("/api/mobile/clear_devices", methods=["POST"])
def handle_clear_mobile_devices():
    """Sprint 12: Clears all dynamically registered devices from the device table."""
    if not mqtt_service:
        return jsonify({"status": "error", "message": ERR_MOBILE_UNAVAILABLE}), 503
    try:
        res = mqtt_service.clear_registered_devices()
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    socketio.emit("mobile_devices_cleared", res, namespace=DASHBOARD_NAMESPACE)
    return jsonify({"status": "success", "active_device": res.get("active_device", ""), "devices": []})

def _get_unified_config():
    base = {}
    if desktop_controller:
        try:
            base = desktop_controller.get_config() or {}
        except Exception:
            base = {}
    base["command_window_seconds"] = fsm_controller.get_window_duration()
    base["volume"] = _get_current_volume()
    return base

def _extract_window_param(data):
    for key in ("command_window_seconds", "window_duration", "window"):
        val = data.get(key)
        if val is not None:
            return val
    return None

def _apply_unified_config_post(data):
    win = _extract_window_param(data)
    updated = {}
    if win is not None:
        new_val = fsm_controller.set_window_duration(float(win))
        updated["command_window_seconds"] = new_val
        socketio.emit("config_update", {"command_window_seconds": new_val}, namespace=DASHBOARD_NAMESPACE)
        socketio.emit("volume_update", {"volume": _volume_state["volume"]}, namespace=DASHBOARD_NAMESPACE)
        logging.getLogger().info(f"[CONFIG] command_window_seconds set to {new_val}s via /api/config")

    if desktop_controller:
        confidence = data.get("confidence_threshold")
        cooldown = data.get("volume_cooldown")
        headless = data.get("headless")
        if any(v is not None for v in (confidence, cooldown, headless)):
            cfg = desktop_controller.update_config(
                confidence_threshold=confidence,
                volume_cooldown=cooldown,
                headless=headless,
            )
            base = cfg
            base["command_window_seconds"] = fsm_controller.get_window_duration()
            base["volume"] = _get_current_volume()
            socketio.emit("config_update", base, namespace=DASHBOARD_NAMESPACE)
            return base

        if updated:
            base = desktop_controller.get_config() or {}
            base.update(updated)
            base["volume"] = _get_current_volume()
            return base

    if updated:
        return {"command_window_seconds": updated["command_window_seconds"], "volume": _get_current_volume()}
    return {"command_window_seconds": fsm_controller.get_window_duration(), "volume": _get_current_volume()}

@app.route("/api/config", methods=["GET", "POST"])
def handle_unified_config():
    if request.method == "GET":
        return jsonify({"status": "success", "config": _get_unified_config()})
    data = request.json or {}
    try:
        cfg = _apply_unified_config_post(data)
        return jsonify({"status": "success", "config": cfg})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

# Legacy helper for direct volume endpoint (desktop path uses /api/volume)
@app.route("/api/volume", methods=["GET", "POST"])
def handle_volume():
    if request.method == "GET":
        return jsonify({"status":"success","volume": _get_current_volume(), "source": _volume_state.get("source","desktop")})
    data = request.json or {}
    vol = data.get("volume")
    if vol is None:
        vol = data.get("value")
    if vol is None:
        return jsonify({"status":"error","message":"Missing volume (0-100)"}),400
    try:
        vol_int = int(max(0, min(100, int(float(vol)))))
        # Use set_system_master_volume via desktop modules if available
        actual = vol_int
        try:
            if DESKTOP_AVAILABLE:
                from os_operations import set_system_master_volume as _set_vol
                actual = int(_set_vol(vol_int))
                # also update controller cached
                try:
                    desktop_controller.current_volume = float(actual)
                except Exception:
                    pass
            else:
                # fallback: just store
                _volume_state["volume"] = vol_int
                actual = vol_int
        except Exception:
            logging.getLogger().exception("[VOLUME] set failed")
            actual = vol_int
            _volume_state["volume"] = actual
        _volume_state["volume"] = actual
        socketio.emit("volume_update", {"volume": actual, "source": "manual"}, namespace=DASHBOARD_NAMESPACE)
        return jsonify({"status":"success","volume": actual})
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}),400

# Ensure unified handlers override legacy desktop handlers for same paths
try:
    # desktop_controller registers handle_config and handle_direct_volume; unify them
    if 'handle_config' in app.view_functions and 'handle_unified_config' in app.view_functions:
        app.view_functions['handle_config'] = app.view_functions['handle_unified_config']
    if 'handle_direct_volume' in app.view_functions and 'handle_volume' in app.view_functions:
        # keep both endpoints pointing to unified volume handler
        app.view_functions['handle_direct_volume'] = app.view_functions['handle_volume']
except Exception:
    pass

# --- PostgreSQL Device Registry & Command History APIs ---
ERR_POSTGRES_UNAVAILABLE = "PostgreSQL not available"
ERR_POSTGRES_NOT_INITIALIZED = "PostgreSQL not initialized"


def _format_pg_datetimes(items, keys=("created_at", "updated_at", "last_seen_at", "executed_at")):
    for item in items:
        for key in keys:
            if item.get(key) and hasattr(item[key], "isoformat"):
                item[key] = item[key].isoformat()
    return items


@app.route("/api/devices", methods=["GET"])
def handle_devices_list():
    """Get all registered devices from PostgreSQL."""
    if not get_postgres_store:
        return jsonify({"status": "error", "message": ERR_POSTGRES_UNAVAILABLE}), 503
    pg = get_postgres_store()
    if not pg:
        return jsonify({"status": "error", "message": ERR_POSTGRES_NOT_INITIALIZED}), 503
    devices = _format_pg_datetimes(pg.get_all_devices())
    return jsonify({"status": "success", "devices": devices})


@app.route("/api/devices/active", methods=["GET"])
def handle_active_devices():
    """Get only active devices from PostgreSQL."""
    if not get_postgres_store:
        return jsonify({"status": "error", "message": ERR_POSTGRES_UNAVAILABLE}), 503
    pg = get_postgres_store()
    if not pg:
        return jsonify({"status": "error", "message": ERR_POSTGRES_NOT_INITIALIZED}), 503
    devices = _format_pg_datetimes(pg.get_active_devices())
    return jsonify({"status": "success", "devices": devices})


@app.route("/api/command_history", methods=["GET"])
def handle_command_history():
    """Get command history from PostgreSQL, optionally filtered by device."""
    if not get_postgres_store:
        return jsonify({"status": "error", "message": ERR_POSTGRES_UNAVAILABLE}), 503
    pg = get_postgres_store()
    if not pg:
        return jsonify({"status": "error", "message": ERR_POSTGRES_NOT_INITIALIZED}), 503
    target_device = request.args.get("device")
    limit = min(int(request.args.get("limit", 100)), 500)
    offset = int(request.args.get("offset", 0))
    history = _format_pg_datetimes(pg.get_command_history(target_device=target_device, limit=limit, offset=offset))
    return jsonify({"status": "success", "history": history, "limit": limit, "offset": offset})


@app.route("/api/system_events", methods=["GET"])
def handle_system_events():
    """Get recent system events from PostgreSQL."""
    if not get_postgres_store:
        return jsonify({"status": "error", "message": ERR_POSTGRES_UNAVAILABLE}), 503
    pg = get_postgres_store()
    if not pg:
        return jsonify({"status": "error", "message": ERR_POSTGRES_NOT_INITIALIZED}), 503
    try:
        with pg._get_connection() as conn:
            with conn.cursor() as cur:
                limit = min(int(request.args.get("limit", 100)), 500)
                event_type = request.args.get("type")
                if event_type:
                    cur.execute(
                        "SELECT id, event_type, severity, source, message, details_json, device_id, created_at "
                        "FROM system_events WHERE event_type = %s ORDER BY created_at DESC LIMIT %s",
                        (event_type, limit),
                    )
                else:
                    cur.execute(
                        "SELECT id, event_type, severity, source, message, details_json, device_id, created_at "
                        "FROM system_events ORDER BY created_at DESC LIMIT %s",
                        (limit,),
                    )
                rows = cur.fetchall()
                events = []
                for r in rows:
                    events.append({
                        "id": r[0],
                        "event_type": r[1],
                        "severity": r[2],
                        "source": r[3],
                        "message": r[4],
                        "details_json": r[5],
                        "device_id": r[6],
                        "created_at": r[7].isoformat() if r[7] else None,
                    })
                return jsonify({"status": "success", "events": events})
    except Exception as e:
        logging.getLogger().exception(f"[POSTGRES] system_events query failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/postgres/health", methods=["GET"])
def handle_postgres_health():
    """PostgreSQL health check endpoint."""
    if not get_postgres_store:
        return jsonify({"healthy": False, "error": ERR_POSTGRES_UNAVAILABLE}), 503
    pg = get_postgres_store()
    if not pg:
        return jsonify({"healthy": False, "error": ERR_POSTGRES_NOT_INITIALIZED}), 503
    health = pg.health_check()
    return jsonify({"status": "success", "health": health})


@app.route("/favicon.ico", methods=["GET"])
def favicon():
    return send_from_directory(app.static_folder, "favicon.svg", mimetype="image/svg+xml")


def _run_start_chrome_blocking():
    """Deterministic startup: run start_chrome.py synchronously and wait.
    In multi-device IoT setups, JioSaavn automation runs on the receiver (PC2),
    not on the transmitter (PC1). Skipped unless AUTO_START_CHROME=true."""
    if os.environ.get("AUTO_START_CHROME", "false").lower() not in ("true", "1", "yes"):
        logging.getLogger().info("[STARTUP] Local Chrome pre-launch skipped (JioSaavn runs on receiver PC2)")
        return False
    logger = logging.getLogger()
    start_chrome_path = DESKTOP_DIR / "start_chrome.py"
    if not start_chrome_path.is_file():
        logger.error(f"[STARTUP] start_chrome.py not found at {start_chrome_path} — skipping Chrome pre-launch")
        return False
    logger.info("[STARTUP] Running start_chrome.py before Flask server...")
    try:
        # Use same interpreter, inherit env, block until completion (~20s max + margin)
        result = subprocess.run(
            [sys.executable, str(start_chrome_path)],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace"
        )
        if result.stdout:
            for line in result.stdout.strip().splitlines()[-20:]:
                logger.info(f"[start_chrome] {line}")
        if result.returncode == 0:
            logger.info("[STARTUP] start_chrome.py completed successfully — Chrome ready, starting Flask server")
            return True
        else:
            if result.stderr:
                for line in result.stderr.strip().splitlines()[-20:]:
                    logger.error(f"[start_chrome][stderr] {line}")
            logger.error(f"[STARTUP] start_chrome.py failed with exit code {result.returncode} — continuing Flask startup anyway")
            return False
    except subprocess.TimeoutExpired:
        logger.error("[STARTUP] start_chrome.py timed out after 60s — continuing Flask startup anyway")
        return False
    except Exception as e:
        logger.exception(f"[STARTUP] Failed to invoke start_chrome.py: {e} — continuing Flask startup anyway")
        return False


def _init_postgres_on_startup(logger):
    if not init_postgres:
        return
    pg_ok = init_postgres()
    if pg_ok:
        logger.info("[POSTGRES] Database connection pool initialized")
        pg = get_postgres_store()
        if pg:
            pg.log_startup("1.0")
    else:
        logger.warning("[POSTGRES] Database unavailable - continuing without persistence")


def _start_background_services(logger):
    if MOBILE_AVAILABLE and mqtt_service:
        logger.info("[UNIFIED] Starting Mobile MQTT service...")
        if not mqtt_service.client or not mqtt_service.client.is_connected():
            threading.Thread(target=mqtt_service.connect, daemon=True, name="MQTTService-Connect").start()
    logger.info("[UNIFIED] Starting Desktop poller...")
    socketio.start_background_task(desktop_status_poller)
    logger.info("[UNIFIED] Starting Unified volume poller...")
    socketio.start_background_task(_volume_poller)
    if CORTEX_ENABLED and cortex_bridge:
        logger.info("[UNIFIED] Starting Cortex bridge...")
        threading.Thread(target=lambda: cortex_bridge.connect(), daemon=True).start()


def main():
    boxed_logger.configure_console()
    logger = logging.getLogger()
    boxed_logger.print_box("SYSTEM STARTUP", [
        ("Time", boxed_logger.now_str()),
        ("Component", "TRANSMITTER"),
        ("Status", "ONLINE"),
        ("PID", os.getpid()),
        ("Log Mode", "Production Structured Logging"),
        ("Log File", boxed_logger.get_log_path()),
    ])
    _init_postgres_on_startup(logger)
    _run_start_chrome_blocking()
    if not DEBUG_MODE or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        _start_background_services(logger)
    port = int(os.environ.get("UNIFIED_PORT", os.environ.get("MASTER_HUB_PORT", "8000")))
    logger.info(f"[UNIFIED] SynaptiMesh Unified Dashboard -> http://localhost:{port}/dashboard")
    try:
        socketio.run(app, host="0.0.0.0", port=port, debug=DEBUG_MODE, allow_unsafe_werkzeug=True)
    finally:
        if close_postgres:
            close_postgres()
            logger.info("[POSTGRES] Connection pool closed")

if __name__=="__main__":
    main()
