"""Hierarchical FSM command interpretation layer for the Master Hub.

Replaces the direct command mapping with a hierarchical Finite State Machine
that interprets the four BCI primitives (``push`` / ``pull`` / ``left`` /
``right``) based on the current state:

- Level 1: DOMAIN_SELECTION
- Level 2: SUB_MASTER_DASHBOARD
- Level 3: MOBILE/DESKTOP_DASHBOARD_ACTIVE + RIGHT_COMBINATION_WAIT

Only the interpretation layer is new; the existing MQTT and Selenium control
paths are reused unchanged via ``command_mapper`` / ``backend.command_router``.
"""

import sys

from .fsm_controller import (
    PUSH,
    PULL,
    LEFT,
    RIGHT,
    PRIMITIVE_COMMANDS,
    DOMAIN_SELECTION,
    SUB_MASTER_DASHBOARD,
    MOBILE_DASHBOARD_ACTIVE,
    DESKTOP_DASHBOARD_ACTIVE,
    LEVEL_3_COMMAND_MODE,
    RIGHT_COMBINATION_WAIT,
    INITIAL_STATE,
    LEVEL_3_STATES,
    SELECT_AI_ML_DOMAIN,
    RETURN_TO_LEVEL_1,
    RETURN_TO_LEVEL_2,
    WAIT_FOR_COMBINATION,
    NONE,
    SELECT_MOBILE_DASHBOARD,
    SELECT_DESKTOP_DASHBOARD,
    OPEN_MOBILE_DASHBOARD,
    OPEN_DESKTOP_DASHBOARD,
    NEXT_TRACK,
    PREVIOUS_TRACK,
    PLAY_PAUSE,
    VOLUME_UP,
    VOLUME_DOWN,
    SEARCH,
    BACK_TO_LEVEL_2,
    BACK_TO_MAIN_DASHBOARD,
    ACTION_LABELS,
    STATE_LABELS,
    FSMController,
    create_fsm,
)
from .command_mapper import CommandMapper, command_mapper
from .cortex_bridge import CortexBridge, create_cortex_bridge

# Backward compatibility alias: any import of 'command_system.states' resolves here
sys.modules["command_system.states"] = sys.modules[__name__]

__all__ = [
    "PUSH",
    "PULL",
    "LEFT",
    "RIGHT",
    "PRIMITIVE_COMMANDS",
    "DOMAIN_SELECTION",
    "SUB_MASTER_DASHBOARD",
    "MOBILE_DASHBOARD_ACTIVE",
    "DESKTOP_DASHBOARD_ACTIVE",
    "LEVEL_3_COMMAND_MODE",
    "RIGHT_COMBINATION_WAIT",
    "INITIAL_STATE",
    "LEVEL_3_STATES",
    "SELECT_AI_ML_DOMAIN",
    "RETURN_TO_LEVEL_1",
    "RETURN_TO_LEVEL_2",
    "WAIT_FOR_COMBINATION",
    "NONE",
    "SELECT_MOBILE_DASHBOARD",
    "SELECT_DESKTOP_DASHBOARD",
    "OPEN_MOBILE_DASHBOARD",
    "OPEN_DESKTOP_DASHBOARD",
    "NEXT_TRACK",
    "PREVIOUS_TRACK",
    "PLAY_PAUSE",
    "VOLUME_UP",
    "VOLUME_DOWN",
    "SEARCH",
    "BACK_TO_LEVEL_2",
    "BACK_TO_MAIN_DASHBOARD",
    "ACTION_LABELS",
    "STATE_LABELS",
    "CommandMapper",
    "command_mapper",
    "FSMController",
    "create_fsm",
    "CortexBridge",
    "create_cortex_bridge",
]
