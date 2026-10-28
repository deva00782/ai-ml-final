"""Hierarchical FSM controller for the Master Hub command system.

The BCI layer only provides four primitives (``push``, ``pull``, ``left``,
``right``). Their meaning depends on the current FSM state:

Level 1  DOMAIN_SELECTION
    right  -> AI/ML domain selected, move to Level 2

Level 2  SUB_MASTER_DASHBOARD
    push   -> select & open Mobile Dashboard (Level 3)
    pull   -> select & open Desktop Dashboard (Level 3)
    (left and right are disabled at Level 2)

Level 3  MOBILE/DESKTOP_DASHBOARD_ACTIVE (media control) - via 4s window
    push   -> Next Track            (single within 4s window)
    pull   -> Previous Track        (single within 4s window)
    left   -> Play / Pause          (single within 4s window)
    right  -> Search                (single within 4s window)
    right + push -> Volume +        (ordered double within same 4s window)
    right + pull -> Volume -        (ordered double within same 4s window)
    push + right -> Back to Sub-Domain  (ordered double)
    push + left  -> Back to AI/ML Domain (ordered double)

Note: RIGHT_COMBINATION_WAIT is legacy prefix path for direct process_command (used by
required flows/tests). Live Cortex path uses true 6-second window grouping owned by FSM
(receive_cortex_command -> 6s timer -> ordered single/double -> mapper -> router).

Every command is logged with the [STATE] / [COMMAND] / [ACTION] markers so the
flow is easy to trace while debugging.
"""

import logging
import threading
import time
from typing import Optional

# ---------------------------------------------------------------------------
# BCI primitive commands (the only things the BCI headset provides)
# ---------------------------------------------------------------------------
PUSH = "push"
PULL = "pull"
LEFT = "left"
RIGHT = "right"

PRIMITIVE_COMMANDS = {PUSH, PULL, LEFT, RIGHT}

# ---------------------------------------------------------------------------
# FSM states
# ---------------------------------------------------------------------------
DOMAIN_SELECTION = "DOMAIN_SELECTION"
SUB_MASTER_DASHBOARD = "SUB_MASTER_DASHBOARD"
MOBILE_DASHBOARD_ACTIVE = "MOBILE_DASHBOARD_ACTIVE"
DESKTOP_DASHBOARD_ACTIVE = "DESKTOP_DASHBOARD_ACTIVE"
LEVEL_3_COMMAND_MODE = "LEVEL_3_COMMAND_MODE"
RIGHT_COMBINATION_WAIT = "RIGHT_COMBINATION_WAIT"

INITIAL_STATE = SUB_MASTER_DASHBOARD  # Application starts at Level 2 (Sub-Master) synchronized with UI

# States in which the FSM is at "Level 3" (media control is active).
LEVEL_3_STATES = {
    MOBILE_DASHBOARD_ACTIVE,
    DESKTOP_DASHBOARD_ACTIVE,
    LEVEL_3_COMMAND_MODE,
}

# ---------------------------------------------------------------------------
# High-level actions the FSM can produce
# ---------------------------------------------------------------------------
# Level 1 / navigation
SELECT_AI_ML_DOMAIN = "select_ai_ml_domain"
RETURN_TO_LEVEL_1 = "return_to_level_1"
RETURN_TO_LEVEL_2 = "return_to_level_2"
WAIT_FOR_COMBINATION = "wait_for_combination"
NONE = "none"

# Level 2 (dashboard selection)
SELECT_MOBILE_DASHBOARD = "select_mobile_dashboard"
SELECT_DESKTOP_DASHBOARD = "select_desktop_dashboard"

# Level 1 -> direct open (auto domain selection + open the dashboard)
OPEN_MOBILE_DASHBOARD = "open_mobile_dashboard"
OPEN_DESKTOP_DASHBOARD = "open_desktop_dashboard"

# Level 3 (media control)
NEXT_TRACK = "next_track"
PREVIOUS_TRACK = "previous_track"
PLAY_PAUSE = "play_pause"
VOLUME_UP = "volume_up"
VOLUME_DOWN = "volume_down"
SEARCH = "search"
BACK_TO_LEVEL_2 = "back_to_level_2"
BACK_TO_MAIN_DASHBOARD = "back_to_main_dashboard"

# Display labels for logs / UI
ACTION_LABELS = {
    SELECT_AI_ML_DOMAIN: "Select AI/ML Domain",
    RETURN_TO_LEVEL_1: "Return to Level 1",
    RETURN_TO_LEVEL_2: "Return to Level 2",
    WAIT_FOR_COMBINATION: "Wait for combination command",
    NONE: "No action",
    SELECT_MOBILE_DASHBOARD: "Select Mobile Dashboard",
    SELECT_DESKTOP_DASHBOARD: "Select Desktop Dashboard",
    OPEN_MOBILE_DASHBOARD: "Open Mobile Dashboard",
    OPEN_DESKTOP_DASHBOARD: "Open Desktop Dashboard",
    NEXT_TRACK: "Next Track",
    PREVIOUS_TRACK: "Previous Track",
    PLAY_PAUSE: "Play / Pause",
    VOLUME_UP: "Volume Up",
    VOLUME_DOWN: "Volume Down",
    SEARCH: "Search",
    BACK_TO_LEVEL_2: "Back to Level 2",
    BACK_TO_MAIN_DASHBOARD: "Back to Main Dashboard",
}

STATE_LABELS = {
    DOMAIN_SELECTION: "DOMAIN_SELECTION (Level 1)",
    SUB_MASTER_DASHBOARD: "SUB_MASTER_DASHBOARD (Level 2)",
    MOBILE_DASHBOARD_ACTIVE: "MOBILE_DASHBOARD_ACTIVE (Level 3)",
    DESKTOP_DASHBOARD_ACTIVE: "DESKTOP_DASHBOARD_ACTIVE (Level 3)",
    LEVEL_3_COMMAND_MODE: "LEVEL_3_COMMAND_MODE (Level 3)",
    RIGHT_COMBINATION_WAIT: "RIGHT_COMBINATION_WAIT (Level 3 prefix)",
}

from .command_mapper import DEVICE_ACTIONS

# Logging constants
STATE_LOG_FMT = "[STATE] New State: %s"


class FSMController:
    """Interprets primitive BCI commands hierarchically and performs actions."""

    # Centralized command window config - 4 seconds per updated spec
    COMMAND_WINDOW_SECONDS = 4.0

    def __init__(self, mapper=None, initial_state=INITIAL_STATE, dry_run=True, combo_timeout=4.0):
        self.mapper = mapper  # CommandMapper used for Level 3 device actions
        self.current_state = initial_state
        self.target_device = None          # "mobile" | "desktop" | None
        self.dashboard_open = False        # True once Start Automation opens one
        self.combination_first = None      # first primitive in RIGHT_COMBINATION_WAIT
        self.return_state = None           # where to go back to after a combo
        self.combo_timeout = float(combo_timeout)  # seconds to wait for 2nd command (4s per spec)
        self.combo_started_at = None       # wall-clock when the prefix command arrived
        self.dry_run = dry_run
        self.history = []                  # list of result dicts
        self.logger = logging.getLogger()
        self.result_callback = None        # Callback for emitting results to UI
        self.logger.info("[FSM] Initial state: Level 2 (Sub-Master) - %s", STATE_LABELS.get(self.current_state, self.current_state))
        # 4-second command window owned by FSM (spec: FSM determines single vs double) - centralized
        self.window_duration = self.COMMAND_WINDOW_SECONDS
        self.window_confidence_threshold = 0.35  # updated per config change
        self.window_commands: list = []          # list of (command, confidence, timestamp)
        self.window_lock = threading.Lock()
        self.window_timer = None
        self.window_generation = 0               # token to invalidate stale timers
        self.window_active = False               # true while a window is open
        # Reuse debounce/cooldown for volume (like BCISignalFilter)
        self.last_volume_time = 0.0
        self.last_volume_action = None
        self.volume_cooldown = 2.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_mapper(self, mapper):
        self.mapper = mapper

    def set_combo_timeout(self, timeout):
        self.combo_timeout = float(timeout)

    def set_window_duration(self, seconds: float):
        """Update command window duration. Safe for live updates.

        Behavior: if a window is currently active, it finishes using its
        original duration snapshot; the new value applies to the next window.
        This avoids inconsistent mid-window timer restart.
        """
        seconds = float(seconds)
        # clamp 0.5 - 10.0 per spec sensible range
        seconds = max(0.5, min(10.0, seconds))
        old = self.window_duration
        self.window_duration = seconds
        self.combo_timeout = seconds  # keep legacy prefix timeout in sync
        self.logger.info(f"[WINDOW] Duration updated: {old:.1f}s -> {seconds:.1f}s (applies next window; active gen {self.window_generation})")
        return seconds

    def get_window_duration(self) -> float:
        return float(self.window_duration)

    def set_result_callback(self, callback):
        """Set callback function to emit results to UI (e.g., SocketIO emit).
        
        Args:
            callback: Function that takes a result dict and emits it to the UI
        """
        self.result_callback = callback

    def _get_level(self, state=None):
        """Return level number for a given state (1,2,3)."""
        s = state or self.current_state
        if s == DOMAIN_SELECTION:
            return 1
        if s == SUB_MASTER_DASHBOARD:
            return 2
        if s in LEVEL_3_STATES or s == RIGHT_COMBINATION_WAIT:
            return 3
        return 2  # default to Level 2 (initial)

    def get_state(self):
        """Snapshot used by the dashboard / tests / logging."""
        return {
            "current_state": self.current_state,
            "state_label": STATE_LABELS.get(self.current_state, self.current_state),
            "target_device": self.target_device,
            "dashboard_open": self.dashboard_open,
            "dry_run": self.dry_run,
            "level": self._get_level(),
            "command_window_seconds": float(self.window_duration),
            "window_duration": float(self.window_duration),
            "window_generation": int(self.window_generation),
        }

    def reset(self, state=INITIAL_STATE):
        self.current_state = state
        self.target_device = None
        self.dashboard_open = False
        self.combination_first = None
        self.return_state = None
        self.combo_started_at = None
        self.history = []
        with self.window_lock:
            self.window_commands = []
            self.window_active = False
            self.window_generation += 1  # invalidate any pending timer
        self.logger.info("[STATE] FSM reset -> %s (window generation %s)", STATE_LABELS.get(state, state), self.window_generation)

    def process_command(self, primitive, confidence=1.0, timestamp=None):
        """Feed one primitive command into the FSM.

        Returns a dict with the interpreted action, the resulting state and a
        human-readable log line, e.g.::

            {"command": "right", "action": "select_ai_ml_domain",
             "new_state": "SUB_MASTER_DASHBOARD", "message": "...",
             "dispatch": {...}}
        """
        start_ts = time.time()
        bci_time_str = time.strftime('%H:%M:%S', time.localtime(timestamp if timestamp else start_ts))
        level_before = self._get_level()
        self.logger.info(f"[LATENCY] BCI received: {bci_time_str}.{int((timestamp if timestamp else start_ts)*1000)%1000:03d}")
        self.logger.info(f"[LATENCY] FSM decision start: {time.strftime('%H:%M:%S', time.localtime(start_ts))}.{int(start_ts*1000)%1000:03d}")
        primitive = (primitive or "").strip().lower()
        if primitive not in (PUSH, PULL, LEFT, RIGHT):
            self.logger.info("[COMMAND] Ignoring unknown primitive: %r", primitive)
            return {
                "command": primitive,
                "action": NONE,
                "new_state": self.current_state,
                "message": f"Ignored unknown command '{primitive}'",
                "dispatch": None,
                "confidence": confidence,
                "level": level_before,
                "timestamp": bci_time_str,
                "latency_ms": 0,
            }

        self.logger.info("[STATE] Current State: %s", STATE_LABELS.get(self.current_state))
        self.logger.info("[COMMAND] Received: %s (confidence=%s)", primitive, confidence)

        result = self._dispatch_primitive(primitive)

        # Enrich result for UI: raw command, confidence, level, timestamp, latency
        result["raw_command"] = primitive.upper()
        result["command_display"] = primitive.upper()
        result["confidence"] = confidence
        result["level"] = level_before
        result["timestamp"] = bci_time_str
        # Latency from BCI received to FSM decision
        latency_ms = int((time.time() - (timestamp if timestamp else start_ts)) * 1000)
        result["latency_ms"] = latency_ms
        result["latency"] = f"{latency_ms} ms"

        self.logger.info(
            "[ACTION] %s", result.get("message") or ACTION_LABELS.get(result.get("action"), "")
        )
        self.logger.info(
            STATE_LOG_FMT, STATE_LABELS.get(self.current_state, self.current_state)
        )
        dispatch_ts = time.time()
        if result.get("dispatch"):
            self.logger.info(f"[LATENCY] Dispatch: {time.strftime('%H:%M:%S', time.localtime(dispatch_ts))}.{int(dispatch_ts*1000)%1000:03d} -> {result.get('dispatch')}")
            self.logger.info(f"[LATENCY] FSM action: {time.strftime('%H:%M:%S', time.localtime(dispatch_ts))}.{int(dispatch_ts*1000)%1000:03d}")

        self.history.append(result)
        
        # Emit result to UI via callback if set (for Cortex commands)
        if self.result_callback:
            try:
                self.result_callback(result)
            except Exception as e:
                self.logger.error(f"[FSM] Result callback error: {e}")
        
        return result

    def start_automation(self):
        """UI action: open the dashboard that was selected at Level 2.

        Returns the device to open, or ``None`` if nothing was selected.
        """
        if self.current_state != SUB_MASTER_DASHBOARD or self.target_device is None:
            self.logger.info(
                "[ACTION] Start Automation ignored (state=%s target=%s)",
                self.current_state,
                self.target_device,
            )
            return None

        device = self.target_device
        self.dashboard_open = True
        self.combination_first = None
        self.return_state = None
        self.combo_started_at = None
        self.current_state = (
            MOBILE_DASHBOARD_ACTIVE if device == "mobile" else DESKTOP_DASHBOARD_ACTIVE
        )
        self.logger.info(
            "[ACTION] Start Automation -> opening %s dashboard (Level 3 media control)",
            device,
        )
        self.logger.info(
            STATE_LOG_FMT, STATE_LABELS.get(self.current_state, self.current_state)
        )
        result = {
            "command": "start_automation",
            "action": f"open_{device}",
            "new_state": self.current_state,
            "message": f"{device} dashboard opened",
            "dispatch": None,
        }
        self.history.append(result)
        return device

    def navigate_back(self):
        """UI action triggered by the 'Back to Master Hub' button.

        Behaves like a 'left' navigation command: from the open dashboard
        (Level 3) it returns to the previous level, i.e. the main dashboard /
        hub starting state (Level 1), so push/pull are ready to open again.
        """
        self.logger.info("[COMMAND] Received: left (back navigation - Back to Master Hub)")
        if (
            self.current_state in LEVEL_3_STATES
            or self.current_state in (SUB_MASTER_DASHBOARD, RIGHT_COMBINATION_WAIT)
        ):
            self.target_device = None
            self.dashboard_open = False
            self.combination_first = None
            self.return_state = None
            self.combo_started_at = None
            self.current_state = DOMAIN_SELECTION
            action = RETURN_TO_LEVEL_1
            message = "Returned to previous level (Level 1 - DOMAIN_SELECTION)"
        else:
            action = NONE
            message = "Already at the top level (Level 1)"
        self.logger.info("[ACTION] %s", message)
        self.logger.info(
            STATE_LOG_FMT, STATE_LABELS.get(self.current_state, self.current_state)
        )
        result = {
            "command": "left",
            "action": action,
            "action_label": ACTION_LABELS.get(action, action),
            "new_state": self.current_state,
            "message": message,
            "dispatch": None,
        }
        self.history.append(result)
        return result

    def process_cortex_command(self, primitive: str, power: float, confidence_threshold: float = 0.35, timestamp: float = None):
        """Legacy direct processing (without windowing). Prefer receive_cortex_command for live flow.

        Kept for backward compat / tests that expect single-command threshold checking.
        Includes NEUTRAL filtering.
        """
        # NEUTRAL must be ignored completely
        if (primitive or "").strip().lower() == "neutral":
            self.logger.info("[FILTER] NEUTRAL ignored in process_cortex_command")
            return None
        # Apply confidence threshold - reject commands below threshold
        if power < confidence_threshold:
            self.logger.info(
                "[CORTEX] Command rejected: %s (power=%.3f < threshold=%.3f)",
                primitive, power, confidence_threshold
            )
            return None
        
        self.logger.info(
            "[CORTEX] Command accepted: %s (power=%.3f >= threshold=%.3f)",
            primitive, power, confidence_threshold
        )
        
        # Use power as confidence for the FSM
        return self.process_command(primitive, confidence=power, timestamp=timestamp)

    # ------------------------------------------------------------------
    # Live Cortex flow: bridge -> FSM -> 4-second window -> mapping -> router
    # Spec requires FSM owns the window. This is the primary entry point for
    # live Cortex data. It applies threshold, then groups into 1-or-2 windows.
    # Updated to 4s window + duplicate suppression for live BCI.
    # ------------------------------------------------------------------
    def receive_cortex_command(self, primitive: str, power: float, timestamp: float = None):
        """Entry point for cortex_bridge: threshold + 4-second windowing.

        Responsibilities per spec:
        - Reuse configured confidence threshold (0.35)
        - Collect into 4-second windows (1 or 2 commands) - max time to recognize double
        - Preserve order (RIGHT+PUSH != PUSH+RIGHT)
        - After window closes, resolve via state-dependent mapper and router

        Returns True if accepted into window, False if rejected.
        """
        if not self._validate_cortex_command(primitive, power):
            return False
        
        primitive = primitive.strip().lower()
        self._log_command_received(primitive, power)
        
        if timestamp is None:
            timestamp = time.time()
            
        pending_double = self._process_window_entry(primitive, power, timestamp)
        
        if pending_double is not None:
            self._flush_combination(pending_double)
            return True
        return True

    def _validate_cortex_command(self, primitive: str, power: float) -> bool:
        """Validate and filter cortex command. Returns True if command should be processed."""
        primitive = (primitive or "").strip().lower()
        
        # NEUTRAL filtering - must be before confidence/window
        if primitive == "neutral":
            self.logger.info(f"[FILTER] NEUTRAL ignored (confidence={power:.3f}) - not entering window/FSM")
            return False
        
        if primitive not in PRIMITIVE_COMMANDS:
            self.logger.warning(f"[CORTEX] Unknown mental command: {primitive}")
            return False
            
        if power < self.window_confidence_threshold:
            self.logger.info(
                f"[FILTER] Command rejected (low confidence): {primitive} (power={power:.3f} < threshold={self.window_confidence_threshold})"
            )
            return False
            
        return True

    def _log_command_received(self, primitive: str, power: float):
        """Log command receipt with latency tracking."""
        bci_ts = time.time()
        self.logger.info(f"[LATENCY] BCI received: {time.strftime('%H:%M:%S', time.localtime(bci_ts))}.{int(bci_ts*1000)%1000:03d}")
        self.logger.info(f"[BCI] Received: {primitive} confidence={power:.2f}")
        self.logger.info(f"[COMMAND] Accepted: {primitive} (power={power:.3f})")
        self.logger.info(f"[LATENCY] Command accepted: {time.strftime('%H:%M:%S', time.localtime(time.time()))}.{int(time.time()*1000)%1000:03d}")

    def _process_window_entry(self, primitive: str, power: float, timestamp: float):
        """Process command into window. Returns combination data if double detected, else None."""
        pending_double = None
        pending_gen = None
        
        with self.window_lock:
            # Dedup: suppress repeated same-command predictions within same window
            if self.window_commands and len(self.window_commands) == 1 and self.window_commands[0][0] == primitive:
                self.logger.info(f"[COMMAND] Duplicate {primitive} suppressed (same as previous within window) - not adding to window")
                return None
                
            if not self.window_commands:
                pending_gen = self._start_new_window(primitive, power, timestamp)
            else:
                pending_gen = self._add_to_existing_window(primitive, power, timestamp)
                
            if pending_gen is not None:
                cmds = [c for c, _, _ in self.window_commands]
                confs = [p for _, p, _ in self.window_commands]
                ts = self.window_commands[0][2]
                pending_double = (cmds, confs, ts, pending_gen)
                
        return pending_double

    def _start_new_window(self, primitive: str, power: float, timestamp: float) -> Optional[int]:
        """Start a new window. Returns generation number."""
        self.window_generation += 1
        cur_gen = self.window_generation
        self.window_active = True
        self.window_commands = [(primitive, power, timestamp)]
        self._start_window_timer(cur_gen)
        self.logger.info(f"[FILTER] Valid command: {primitive} (power={power:.3f}) -> [WINDOW] Added to window gen={cur_gen} (total=1)")
        self.logger.info(f"[WINDOW] Started gen={cur_gen} - Timeout: {self.window_duration:.1f}s")
        return None  # No combination yet

    def _add_to_existing_window(self, primitive: str, power: float, timestamp: float) -> Optional[int]:
        """Add command to existing window. Returns generation if combination detected."""
        if len(self.window_commands) >= 2:
            self.logger.warning(f"[WINDOW] Window full (2 commands), ignoring: {primitive}")
            return None
            
        if self.window_commands[0][0] == primitive:
            self.logger.info(f"[COMMAND] Duplicate {primitive} suppressed")
            return None
            
        self.window_commands.append((primitive, power, timestamp))
        self.logger.info(f"[WINDOW] Received: {primitive} (total={len(self.window_commands)}) within window gen={self.window_generation}")
        self.logger.info(f"[COMMAND] Double command detected: {self.window_commands[0][0]} + {primitive}")
        
        # For intentional double with different commands, execute immediately
        if len(self.window_commands) == 2 and self.window_commands[0][0] != self.window_commands[1][0]:
            pending_gen = self.window_generation
            self.logger.info(f"[WINDOW] Combination detected: {self.window_commands[0][0]} + {primitive} (gen={pending_gen})")
            self.logger.info(f"[WINDOW] Cancelling pending single-command timer for gen={pending_gen}")
            self.logger.info(f"[WINDOW] Flushing combination: {self.window_commands[0][0]} + {primitive}")
            return pending_gen
            
        return None

    def _flush_combination(self, pending_double):
        """Flush detected combination from window and process it."""
        cmds, confs, ts, pending_gen = pending_double
        self.window_commands = []
        self.window_active = False
        self.logger.info(f"[WINDOW] Window cleared gen={pending_gen}")
        
        result = self.process_command_sequence(cmds, confs, timestamp=ts)
        if result is not None and len(cmds) == 2 and result not in self.history:
            self.history.append(result)
        if self.result_callback and result is not None and result.get("action") not in (None,):
            if len(cmds) == 2:
                try:
                    self.result_callback(result)
                except Exception as e:
                    self.logger.error(f"[FSM] Result callback error: {e}")

    def _start_window_timer(self, generation: int):
        """Start timer for the given window generation. Captures duration snapshot."""
        snap_duration = float(self.window_duration)
        self.logger.info(f"[WINDOW] Started gen={generation} (duration={snap_duration:.1f}s) Timeout: {snap_duration:.1f}s")
        def _timer(my_gen=generation, dur=snap_duration):
            time.sleep(dur)
            with self.window_lock:
                if self.window_generation != my_gen:
                    self.logger.info(f"[WINDOW] Stale timer ignored gen={my_gen} != current gen={self.window_generation}")
                    return
                if not self.window_active:
                    self.logger.info(f"[WINDOW] Stale timer ignored gen={my_gen} (window inactive)")
                    return
                has_pending = bool(self.window_commands)
                if not has_pending:
                    self.logger.info(f"[WINDOW] Timer gen={my_gen} woke but window empty - no flush")
                    return
                self.logger.info(f"[WINDOW] Window timeout after {dur:.1f}s gen={my_gen}, flushing")
            # flush outside lock
            self._flush_window()
        self.window_timer = threading.Thread(target=_timer, daemon=True)
        self.window_timer.start()

    def _flush_window(self):
        """Flush current window and resolve via mapping. Called with generation validated."""
        with self.window_lock:
            if not self.window_commands:
                self.logger.info("[WINDOW] _flush_window called but window_commands empty - no action")
                return
            if not self.window_active:
                self.logger.info(f"[WINDOW] _flush_window stale gen={self.window_generation} inactive - ignored")
                return
            cmds = [c for c, _, _ in self.window_commands]
            confs = [p for _, p, _ in self.window_commands]
            ts = self.window_commands[0][2]
            gen = self.window_generation
            self.logger.info(f"[WINDOW] Window closed gen={gen}: {len(cmds)} command(s) collected: {cmds}")
            self.window_commands = []
            self.window_active = False
            self.logger.info(f"[WINDOW] Window cleared gen={gen}")
        # process outside lock
        result = self.process_command_sequence(cmds, confs, timestamp=ts)
        # Ensure double-line results are recorded in history (single already via process_command)
        if result is not None and len(cmds) == 2 and result not in self.history:
            # _process_double_command does not append to history, do it here
            self.history.append(result)
        # For double-line path, result_callback not yet called inside sequence; ensure callback
        if self.result_callback and result is not None and result.get("action") not in (None,):
            # process_command already called callback for single path; double path needs it
            # Avoid double-emit: process_command_sequence single already emitted, double hasn't
            if len(cmds) == 2:
                try:
                    self.result_callback(result)
                except Exception as e:
                    self.logger.error(f"[FSM] Result callback error: {e}")
        # Note: caller still holds lock; it will exit with block
        # To avoid double-release issues, we manually manage: caller is _flush_window_locked itself
        # This method is always called with lock held; after processing we keep it held
        # The above release/acquire keeps it held for caller

    def process_command_sequence(self, commands: list, confidences: list = None, timestamp: float = None):
        """Process a command sequence from the command window.
        
        This method handles both single-line and double-line commands based on
        the number of commands received in the window.
        
        Args:
            commands: List of commands (e.g., ["push"] or ["right", "push"])
            confidences: List of confidence values for each command
            timestamp: Optional timestamp for the sequence
            
        Returns:
            Result dict from the FSM processing
        """
        if not commands:
            self.logger.warning("[WINDOW] Empty command sequence")
            return {
                "commands": [],
                "action": NONE,
                "new_state": self.current_state,
                "message": "Empty command sequence",
                "dispatch": None,
            }
        
        if confidences is None:
            confidences = [1.0] * len(commands)
        
        # Use the highest confidence from the sequence
        max_confidence = max(confidences) if confidences else 1.0
        
        self.logger.info(
            f"[WINDOW] Processing command sequence: {commands} (max_confidence={max_confidence:.3f})"
        )
        
        # Handle based on number of commands
        if len(commands) == 1:
            return self._process_single_command(commands[0], max_confidence, timestamp)
        elif len(commands) == 2:
            return self._process_double_command(commands, max_confidence, timestamp)
        else:
            self.logger.warning(f"[WINDOW] Unexpected command count: {len(commands)}")
            return {
                "commands": commands,
                "action": NONE,
                "new_state": self.current_state,
                "message": f"Unexpected command count: {len(commands)}",
                "dispatch": None,
            }
    
    def _process_single_command(self, command: str, confidence: float, timestamp: float = None):
        """Process a single-line command.
        
        Args:
            command: Single command (push/pull/left/right)
            confidence: Command confidence
            timestamp: Optional timestamp
            
        Returns:
            Result dict from FSM processing
        """
        self.logger.info(f"[WINDOW] Single-line command: {command}")
        return self.process_command(command, confidence=confidence, timestamp=timestamp)
    
    def _process_double_command(self, commands: list, confidence: float, timestamp: float = None):
        """Process a double-line command combination.
        
        Args:
            commands: List of 2 commands in order received
            confidence: Command confidence
            timestamp: Optional timestamp
            
        Returns:
            Result dict from FSM processing
        """
        cmd1, cmd2 = commands[0], commands[1]
        combination = f"{cmd1} + {cmd2}"
        level_before = self._get_level()
        bci_time_str = time.strftime('%H:%M:%S', time.localtime(timestamp if timestamp else time.time()))
        
        self.logger.info(f"[WINDOW] Double-line command: {combination}")
        
        # Latency for double
        self.logger.info(f"[LATENCY] BCI received: {bci_time_str}.{int((timestamp if timestamp else time.time())*1000)%1000:03d}")
        
        # Map the combination to an action based on current FSM state
        action = self._map_double_command(combination)
        
        if action == NONE:
            self.logger.warning(f"[WINDOW] Unknown combination: {combination}")
            return {
                "commands": commands,
                "command": combination,
                "raw_command": combination.upper(),
                "command_display": combination.upper(),
                "action": NONE,
                "new_state": self.current_state,
                "message": f"Unknown combination: {combination}",
                "dispatch": None,
                "confidence": confidence,
                "level": level_before,
                "timestamp": bci_time_str,
                "latency_ms": int((time.time() - (timestamp if timestamp else time.time()))*1000),
                "latency": f"{int((time.time() - (timestamp if timestamp else time.time()))*1000)} ms",
            }
        
        # Execute the action through the appropriate level handler
        result = self._execute_action(action, confidence, timestamp, combination=combination)
        
        self.logger.info(
            f"[ACTION] {combination} → {ACTION_LABELS.get(action, action)}"
        )
        # Enrich double result for UI
        result["raw_command"] = combination.upper()
        result["command_display"] = combination.upper()
        result["command"] = combination
        result["confidence"] = confidence
        result["level"] = level_before
        result["timestamp"] = bci_time_str
        latency_ms = int((time.time() - (timestamp if timestamp else time.time()))*1000)
        result["latency_ms"] = latency_ms
        result["latency"] = f"{latency_ms} ms"
        self.logger.info(f"[LATENCY] Dispatch: {time.strftime('%H:%M:%S', time.localtime(time.time()))}.{int(time.time()*1000)%1000:03d} -> {result.get('dispatch')}")
        return result
    
    def _map_double_command(self, combination: str) -> str:
        """Map a double-line command combination to an action.
        
        The mapping depends on the current FSM state/level.
        Combinations only work at Level 3 (media control states).
        
        Args:
            combination: The combination string (e.g., "right + push")
            
        Returns:
            Action constant or NONE if unknown or wrong state
        """
        # Define the combination mappings (order-sensitive)
        combination_mappings = {
            "right + push": VOLUME_UP,
            "right + pull": VOLUME_DOWN,
            "push + right": BACK_TO_LEVEL_2,
            "push + left": BACK_TO_MAIN_DASHBOARD,  # Back to AI/ML Domain Selection
        }
        
        # Navigation back combinations work from Level 3 and ensure teardown even if at Level 2
        if combination == "push + right":
            return BACK_TO_LEVEL_2
        if combination == "push + left":
            return BACK_TO_MAIN_DASHBOARD

        # Audio/volume combinations only work at Level 3
        if self.current_state not in LEVEL_3_STATES:
            self.logger.info(
                f"[WINDOW] Combination {combination} ignored - not at Level 3 (current state: {self.current_state})"
            )
            return NONE
        
        # Check if this combination is defined
        if combination in combination_mappings:
            return combination_mappings[combination]
        
        return NONE
    
    def _execute_action(self, action: str, confidence: float, timestamp: float = None, combination: str = None) -> dict:
        """Execute an action (for double-line commands).
        
        Args:
            action: The action to execute
            confidence: Command confidence
            timestamp: Optional timestamp
            
        Returns:
            Result dict
        """
        # Debounce volume actions (reuse existing cooldown from bci_pipeline - only same action)
        if action in (VOLUME_UP, VOLUME_DOWN):
            now = timestamp or time.time()
            if action == self.last_volume_action and (now - self.last_volume_time) < self.volume_cooldown:
                self.logger.info(f"[DEBOUNCE] {action} debounced ({now - self.last_volume_time:.1f}s < {self.volume_cooldown}s)")
                return self._result("combination", NONE, f"{action} debounced (cooldown)", None)
            self.last_volume_time = now
            self.last_volume_action = action
        # Handle special navigation actions
        if action == BACK_TO_LEVEL_2:
            self.target_device = None
            self.dashboard_open = False
            self.combination_first = None
            self.return_state = None
            self.combo_started_at = None
            self.current_state = SUB_MASTER_DASHBOARD
            return self._result("combination", action, "Returned to Level 2 (SUB_MASTER_DASHBOARD)")
        
        if action in (RETURN_TO_LEVEL_1, BACK_TO_MAIN_DASHBOARD):
            self.target_device = None
            self.dashboard_open = False
            self.combination_first = None
            self.return_state = None
            self.combo_started_at = None
            self.current_state = DOMAIN_SELECTION
            return self._result("combination", action, "Returned to Level 1 (DOMAIN_SELECTION)")
        
        # Handle Level 3 actions through existing mechanism
        if action in DEVICE_ACTIONS and self.current_state in LEVEL_3_STATES:
            if self.target_device is not None:
                dispatch = self.mapper.dispatch(action, self.target_device)
            else:
                self.logger.info(f"[ACTION] {action} skipped - no dashboard selected")
                dispatch = None
            
            return {
                "command": "combination",
                "action": action,
                "action_label": ACTION_LABELS.get(action, action),
                "new_state": self.current_state,
                "message": ACTION_LABELS.get(action, action),
                "dispatch": dispatch,
            }
        
        # If action doesn't match current state, return no action
        return self._result("combination", NONE, f"Action '{action}' not valid in current state", None)

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------
    def _dispatch_primitive(self, primitive):
        # A pending combination only stays valid for combo_timeout seconds.
        # If the second command arrives too late, the stale 'right' prefix is
        # dropped and the new command is interpreted normally in its own state.
        if self.current_state == RIGHT_COMBINATION_WAIT:
            self._expire_stale_combo()

        if self.current_state == DOMAIN_SELECTION:
            return self._handle_domain_selection(primitive)
        if self.current_state == SUB_MASTER_DASHBOARD:
            return self._handle_sub_master(primitive)
        if self.current_state == RIGHT_COMBINATION_WAIT:
            return self._handle_combination(primitive)
        if self.current_state in LEVEL_3_STATES:
            return self._handle_level3(primitive)
        # Unknown state: recover safely to the initial state.
        self.logger.warning("[STATE] Unknown state %r, resetting", self.current_state)
        self.current_state = DOMAIN_SELECTION
        return {
            "command": primitive,
            "action": NONE,
            "new_state": self.current_state,
            "message": f"Recovered from unknown state with '{primitive}'",
            "dispatch": None,
        }

    def _enter_combination_wait(self, return_state):
        """Enter RIGHT_COMBINATION_WAIT after a 'right' prefix command."""
        self.current_state = RIGHT_COMBINATION_WAIT
        self.combination_first = RIGHT
        self.return_state = return_state
        self.combo_started_at = time.time()

    def _expire_stale_combo(self):
        """Drop a pending combination whose second command never arrived."""
        if self.combo_started_at is None:
            return
        if time.time() - self.combo_started_at <= self.combo_timeout:
            return
        previous = self.return_state or self._level3_base()
        self.logger.info(
            "[COMMAND] Combination timeout after %.1fs - pending 'right' dropped",
            self.combo_timeout,
        )
        self.combination_first = None
        self.return_state = None
        self.combo_started_at = None
        self.current_state = previous

    def _handle_domain_selection(self, primitive):
        if primitive == RIGHT:
            self.current_state = SUB_MASTER_DASHBOARD
            return self._result(
                primitive, SELECT_AI_ML_DOMAIN, "AI/ML domain selected", None
            )
        # Direct open: PUSH opens the Mobile dashboard and PULL opens the
        # Desktop dashboard straight from the start (AI/ML domain is implied).
        # This is what the operator expects from the control panel; the Level 2
        # selection flow (right -> push/pull -> Start Automation) still exists.
        if primitive == PUSH:
            self.target_device = "mobile"
            self.dashboard_open = True
            self.current_state = MOBILE_DASHBOARD_ACTIVE
            return self._result(
                primitive, OPEN_MOBILE_DASHBOARD, "Mobile Dashboard opened (Level 3)", None
            )
        if primitive == PULL:
            self.target_device = "desktop"
            self.dashboard_open = True
            self.current_state = DESKTOP_DASHBOARD_ACTIVE
            return self._result(
                primitive, OPEN_DESKTOP_DASHBOARD, "Desktop Dashboard opened (Level 3)", None
            )
        self.logger.info("[ACTION] Waiting for 'right' to select AI/ML domain")
        return self._result(primitive, NONE, "Ignored (AI/ML domain not selected yet)", None)

    def _handle_sub_master(self, primitive):
        if primitive == PUSH:
            # Level 2 PUSH -> Mobile Dashboard: directly open (reuse existing OPEN mechanism, do NOT start JSON loop)
            # This reuses the same mechanism as Level 1 direct open, which triggers frontend navigation via open_mobile_dashboard
            self.target_device = "mobile"
            self.dashboard_open = True
            self.combination_first = None
            self.return_state = None
            self.combo_started_at = None
            self.current_state = MOBILE_DASHBOARD_ACTIVE
            return self._result(
                primitive,
                OPEN_MOBILE_DASHBOARD,
                "Mobile Dashboard opened (Level 3) via Level 2 PUSH",
                None,
            )
        if primitive == PULL:
            self.target_device = "desktop"
            self.dashboard_open = True
            self.combination_first = None
            self.return_state = None
            self.combo_started_at = None
            self.current_state = DESKTOP_DASHBOARD_ACTIVE
            return self._result(
                primitive,
                OPEN_DESKTOP_DASHBOARD,
                "Desktop Dashboard opened (Level 3) via Level 2 PULL",
                None,
            )
        if primitive in (LEFT, RIGHT):
            # Spec: LEFT and RIGHT completely removed/ignored at Level 2
            self.logger.info("[ACTION] %s ignored at Level 2 (disabled per spec)", primitive)
            return self._result(primitive, NONE, f"Ignored ({primitive} disabled at Level 2)", None)
        return self._result(primitive, NONE, f"Unknown command {primitive} at Level 2", None)

    def _handle_level3(self, primitive):
        if primitive == PUSH:
            return self._execute_level3(NEXT_TRACK, primitive, "Next Track")
        if primitive == PULL:
            return self._execute_level3(PREVIOUS_TRACK, primitive, "Previous Track")
        if primitive == LEFT:
            return self._execute_level3(PLAY_PAUSE, primitive, "Play / Pause")
        if primitive == RIGHT:
            return self._execute_level3(SEARCH, primitive, "Search")
        
        # Should not reach here, but handle gracefully
        return self._result(primitive, NONE, "Unknown command in Level 3", None)

    def _handle_combination(self, primitive):
        # Only 'right' is expected as the prefix; anything else just resolves
        # the pending combination below.
        second = primitive

        # right + push   -> Volume Up
        if second == PUSH:
            result = self._execute_level3(VOLUME_UP, second, "Volume Up")
            return result

        # right + pull   -> Volume Down
        if second == PULL:
            result = self._execute_level3(VOLUME_DOWN, second, "Volume Down")
            return result

        # right + right  -> Search (legacy prefix, still supported for backward compat)
        if second == RIGHT:
            result = self._execute_level3(SEARCH, second, "Search")
            return result
        # right + left   -> Back to Level 2 / Return to Level 1 (legacy)
        # Keep for backward compat with existing run_fsm_tests; new window uses PUSH+LEFT for this
        if second == LEFT:
            # If we were waiting from Level 3, go back to Level 2; if from Level 2, stay at Level 2 per spec
            if self.return_state in LEVEL_3_STATES or self.return_state == SUB_MASTER_DASHBOARD:
                self.target_device = None
                self.dashboard_open = False
                self.combination_first = None
                self.return_state = None
                self.combo_started_at = None
                self.current_state = SUB_MASTER_DASHBOARD
                return self._result(second, RETURN_TO_LEVEL_2, "Return to Level 2 (via right+left legacy)", None)
            return self._result(second, RETURN_TO_LEVEL_1, "Return to Level 1", None)
        
        # Any other second command cancels the combination wait
        self.combination_first = None
        self.combo_started_at = None
        return self._result(
            second, NONE, f"Combination 'right + {second}' not supported", None
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _execute_level3(self, action, primitive, label):
        """Run a Level 3 device action and return to the appropriate state."""
        # Reuse debounce for volume (prevents sustained mental state flooding) - only same action
        if action in (VOLUME_UP, VOLUME_DOWN):
            now = time.time()
            if action == self.last_volume_action and (now - self.last_volume_time) < self.volume_cooldown:
                self.logger.info(f"[DEBOUNCE] {action} debounced ({now - self.last_volume_time:.1f}s < {self.volume_cooldown}s)")
                return self._result(primitive, NONE, f"{action} debounced (cooldown)", None)
            self.last_volume_time = now
            self.last_volume_action = action
        dispatch = None
        if self.mapper is not None and action in DEVICE_ACTIONS:
            if self.target_device is not None:
                dispatch = self.mapper.dispatch(action, self.target_device)
            else:
                self.logger.info(
                    "[ACTION] %s skipped - no dashboard selected", label
                )

        # Resolve back out of the combination wait.
        if self.current_state == RIGHT_COMBINATION_WAIT:
            self.current_state = self.return_state or self._level3_base()
        self.combination_first = None
        self.return_state = None
        self.combo_started_at = None

        return {
            "command": primitive,
            "action": action,
            "action_label": label,
            "new_state": self.current_state,
            "message": label,
            "dispatch": dispatch,
        }

    def _level3_base(self):
        if self.target_device == "mobile":
            return MOBILE_DASHBOARD_ACTIVE
        if self.target_device == "desktop":
            return DESKTOP_DASHBOARD_ACTIVE
        return LEVEL_3_COMMAND_MODE

    def _result(self, command, action, message, dispatch=None):
        return {
            "command": command,
            "action": action,
            "action_label": ACTION_LABELS.get(action, action),
            "new_state": self.current_state,
            "message": message,
            "dispatch": dispatch,
        }


def create_fsm(mapper=None, dry_run=True, combo_timeout=4.0):
    """Convenience factory that also creates a default mapper if needed."""
    from .command_mapper import CommandMapper

    if mapper is None:
        mapper = CommandMapper(dry_run=dry_run)
    return FSMController(mapper=mapper, dry_run=dry_run, combo_timeout=combo_timeout)