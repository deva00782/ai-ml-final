import os
from pathlib import Path
try:
    from dotenv import load_dotenv
    _curr = Path(__file__).resolve().parent
    for _ in range(5):
        _env = _curr / ".env"
        if _env.is_file():
            load_dotenv(_env, override=True)
            break
        _curr = _curr.parent
except ImportError:
    pass

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
DEBUG_MODE = True

MQTT_BROKER = os.environ.get("MQTT_BROKER", "52.21.249.6")
MQTT_FALLBACK_BROKER = os.environ.get("MQTT_FALLBACK_BROKER", "broker.emqx.io")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
MQTT_QOS = 1
MQTT_KEEPALIVE = 60
CLIENT_ID_PREFIX = "BCI-WEB"

MQTT_NAMESPACE = "rohan"
TOPIC_DISCOVERY = "aiml/devices/register"

TOPIC_COMMAND = f"aiml/{MQTT_NAMESPACE}/commands"
TOPIC_IOS_COMMAND = f"{TOPIC_COMMAND}/ios"
TOPIC_CONTROL = f"aiml/{MQTT_NAMESPACE}/control"
TOPIC_ACK = f"aiml/{MQTT_NAMESPACE}/ack"
TOPIC_STATUS = f"aiml/{MQTT_NAMESPACE}/status"
# Sub-Master liveness. iOS watches this topic (not the per-device status topic,
# which is the phone's own presence) and marks the server offline if it goes quiet.
SERVER_STATUS_TOPIC = "aiml/status"
TOPIC_HEARTBEAT = f"aiml/{MQTT_NAMESPACE}/heartbeat"
TOPIC_MEDIA = f"aiml/{MQTT_NAMESPACE}/media"

ONLINE_PAYLOAD = '{"status":"ONLINE"}'
LAST_WILL_PAYLOAD = '{"status":"OFFLINE"}'

def get_device_topics(namespace):
    """Sprint 12: per-device topic helper for multi-device MQTT registry."""
    ns = (namespace or MQTT_NAMESPACE).strip().lower()
    return {
        "command": f"aiml/{ns}/commands",
        "ios_command": f"aiml/{ns}/commands/ios",
        "control": f"aiml/{ns}/control",
        "ack": f"aiml/{ns}/ack",
        "status": f"aiml/{ns}/status",
        "heartbeat": f"aiml/{ns}/heartbeat",
        "media": f"aiml/{ns}/media",
    }

CONFIDENCE_THRESHOLD = 0.80
TARGET_DOMAIN = "AI_ML"

# Calculate DATA_FILE_PATH relative to the project root
# Sprint 11 path (Python_pipeline) did not exist; Sprint 12 fix points to the
# real desktop integrated_eeg.json, with fallback to <root>/data/integrated_eeg.json.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CANDIDATE_DATA_PATH = _PROJECT_ROOT / "desktop" / "data" / "integrated_eeg.json"
DATA_FILE_PATH = _CANDIDATE_DATA_PATH if _CANDIDATE_DATA_PATH.exists() else (_PROJECT_ROOT / "data" / "integrated_eeg.json")
# Legacy alias kept for backward compatibility
PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent

# Command names must match Python_pipeline/config.py for Android compatibility
# Sprint 12 additions (Close_App aliases, SEARCH, raw primitive aliases) are
# additive and keep all Sprint 11 keys unchanged.
COMMAND_MAP = {
    "Right": "Right",
    "Right_jiosaavn": "Right_jiosaavn",
    "Right_Play_Pause": "Right_Play_Pause",
    "Right_Next_Song": "Right_Next_Song",
    "Right_Previous_Song": "Right_Previous_Song",
    "Right_Volume_Up": "Right_Volume_Up",
    "Right_Volume_Down": "Right_Volume_Down",
    "Right_Search_Playlist": "Right_Search_Playlist",
    "Drop": "Drop",
    "Right_Return_to_Home": "Right_Return_to_Home",
    "Right_Close_App": "Right_Close_App",
    "Close_App": "Right_Close_App",
    "Close_JioSaavn": "Right_Close_App",
    "SEARCH": "SEARCH",
    "type_query": "type_query",
    # Raw primitives & action aliases
    "LEFT": "Right_Play_Pause",
    "PUSH": "Right_Next_Song",
    "PULL": "Right_Previous_Song",
    "RIGHT+PUSH": "Right_Volume_Up",
    "RIGHT+PULL": "Right_Volume_Down",
    "PUSH+RIGHT": "Right_Close_App",
    "Back to Level 2": "Right_Close_App",
    "back_to_level_2": "Right_Close_App",
    "Play / Pause": "Right_Play_Pause",
    "Next Track": "Right_Next_Song",
    "Previous Track": "Right_Previous_Song",
    "Volume Up": "Right_Volume_Up",
    "Volume Down": "Right_Volume_Down",
    "Search": "Right_Search_Playlist",
    "Search Album/Playlist": "Right_Search_Playlist",
}

DISPLAY_LABELS = {
    "Right": "Right Gesture",
    "Right_jiosaavn": "Open JioSaavn",
    "Right_Play_Pause": "Play / Pause",
    "Right_Next_Song": "Next Track",
    "Right_Previous_Song": "Previous Track",
    "Right_Volume_Up": "Volume +",
    "Right_Volume_Down": "Volume -",
    "Right_Search_Playlist": "Search Playlist",
    "Drop": "Return to Home",
    "Right_Return_to_Home": "Return to Home",
    "Right_Close_App": "Close JioSaavn",
    "SEARCH": "Search",
    "type_query": "Search Query",
    "LEFT": "Play / Pause",
    "PUSH": "Next Track",
    "PUSH+RIGHT": "Back to Level 2",
    "PULL": "Previous Track",
    "RIGHT+PUSH": "Volume +",
    "RIGHT+PULL": "Volume -",
}
