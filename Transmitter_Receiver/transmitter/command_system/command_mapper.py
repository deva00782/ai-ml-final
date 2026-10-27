"""Map FSM actions to the existing device command names and dispatch them.

This layer reuses the existing single-target dispatch in
``backend/command_router.py`` (which itself calls ``mqtt.publish_command`` for
mobile and ``controller.execute_action`` for desktop), so the underlying
JioSaavn control functionality is untouched. A ``dry_run`` mode lets the FSM be
exercised headlessly (no MQTT, no Selenium) for testing.
"""

from .fsm_controller import (
    NEXT_TRACK,
    PREVIOUS_TRACK,
    PLAY_PAUSE,
    VOLUME_UP,
    VOLUME_DOWN,
    SEARCH,
    BACK_TO_LEVEL_2,
    BACK_TO_MAIN_DASHBOARD,
)

# ---------------------------------------------------------------------------
# Device-specific command names for each dashboard.
# Mobile expects the MQTT names (COMMAND_MAP in mobile config); desktop expects
# the normalized action names (COMMAND_MAPPING in desktop bci_pipeline).
# ---------------------------------------------------------------------------
DEVICE_ACTION_MAP = {
    "mobile": {
        NEXT_TRACK: "Right_Next_Song",
        PREVIOUS_TRACK: "Right_Previous_Song",
        PLAY_PAUSE: "Right_Play_Pause",
        VOLUME_UP: "Right_Volume_Up",
        VOLUME_DOWN: "Right_Volume_Down",
        SEARCH: "Right_Search_Playlist",
        BACK_TO_LEVEL_2: "Right_Close_App",
    },
    "ios": {
        NEXT_TRACK: "Right_Next_Song",
        PREVIOUS_TRACK: "Right_Previous_Song",
        PLAY_PAUSE: "Right_Play_Pause",
        VOLUME_UP: "Right_Volume_Up",
        VOLUME_DOWN: "Right_Volume_Down",
        SEARCH: "Right_Search_Playlist",
        BACK_TO_LEVEL_2: "Right_Close_App",
    },
    "desktop": {
        NEXT_TRACK: "Next Track",
        PREVIOUS_TRACK: "Previous Track",
        PLAY_PAUSE: "Play / Pause",
        VOLUME_UP: "Volume Up",
        VOLUME_DOWN: "Volume Down",
        SEARCH: "Search Album/Playlist",
        BACK_TO_LEVEL_2: "Right_Close_App",
    },
}

# FSM actions that correspond to a real device command (everything else is
# purely a navigation/selection action and never dispatched to a device).
DEVICE_ACTIONS = {NEXT_TRACK, PREVIOUS_TRACK, PLAY_PAUSE, VOLUME_UP, VOLUME_DOWN, SEARCH, BACK_TO_LEVEL_2}


def canonical_action_for(command, device):
    """Resolve a direct device command through the existing canonical map."""
    if command in DEVICE_ACTIONS:
        return command
    for action, device_command in DEVICE_ACTION_MAP.get(device, {}).items():
        if command == device_command:
            return action
    return None


class CommandMapper:
    """Translates FSM actions into the target device's command and dispatches."""

    def __init__(self, command_router=None, dry_run=False):
        self.command_router = command_router
        self.dry_run = dry_run
        self.dispatch_log = []

    def set_router(self, command_router):
        self.command_router = command_router

    def set_dry_run(self, dry_run):
        self.dry_run = dry_run

    def resolve(self, action, device):
        """Return the device-specific command string for an FSM action."""
        if action not in DEVICE_ACTIONS or device not in DEVICE_ACTION_MAP:
            return None
        return DEVICE_ACTION_MAP[device][action]

    def dispatch(self, action, device):
        """Dispatch an FSM action to the given device (mobile | desktop).

        Returns a dict describing the dispatch result. Never raises; if the
        backend is not registered the existing router reports
        ``dispatched: False`` instead of crashing.
        """
        record = {
            "action": action,
            "device": device,
            "dispatched": False,
            "reason": None,
            "command": None,
        }

        command = self.resolve(action, device)
        record["command"] = command

        if command is None:
            record["reason"] = f"action '{action}' has no mapping for device '{device}'"
            self.dispatch_log.append(record)
            return record

        if self.dry_run or self.command_router is None:
            record["dispatched"] = True
            record["reason"] = "dry_run" if self.dry_run else "no router (dry run)"
            self.dispatch_log.append(record)
            return record

        try:
            result = self.command_router.route(command, target=device)
            record["dispatched"] = bool(result.get("dispatched", False))
            record["reason"] = result.get("reason") or result.get("result") or "ok"
        except Exception as exc:  # never let a device failure break the FSM
            record["reason"] = f"dispatch error: {exc}"

        self.dispatch_log.append(record)
        return record


command_mapper = CommandMapper(dry_run=True)