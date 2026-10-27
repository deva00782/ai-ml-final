import json
import time
import threading
from pathlib import Path
from logger import logger

DATA_FILE_PATH = Path(__file__).parent / "data" / "integrated_eeg.json"
TARGET_DOMAIN = "AI_ML"
CONFIDENCE_THRESHOLD = 0.80

SONG_QUEUE = [
    "Hukum Jailer",
    "Starboy",
    "Arijit Singh Sad Songs",
    "Devara Chuttamalle",
    "Naatu Naatu",
    "Beauty and a Beat"
]

RANDOM_SONGS_POOL = [
    "Starboy",
    "Hukum Jailer",
    "Devara Chuttamalle",
    "Arijit Singh Sad Songs",
    "Naatu Naatu",
    "Beauty and a Beat",
    "Believer Imagine Dragons",
    "Shape of You Ed Sheeran",
    "Blinding Lights The Weeknd",
    "Kesariya Brahmastra",
    "Tauba Tauba Bad Newz",
    "Apna Bana Le Bhediya",
    "Illuminati Aavesham",
    "Chaleya Jawan",
    "Levitating Dua Lipa",
    "O Maahi Dunki",
    "Calm Down Rema",
    "Peaches Justin Bieber",
    "Arabic Kuthu Beast",
    "Espresso Sabrina Carpenter"
]

COMMAND_MAPPING = {
    "Right": "Select AI/ML Domain",
    "Right_jiosaavn": "Open JioSaavn",
    "Right_Play_Pause": "Play / Pause",
    "Right_Next_Song": "Next Track",
    "Right_Previous_Song": "Previous Track",
    "Right_Volume_Up": "Volume Up",
    "Right_Volume_Down": "Volume Down",
    "Right_Search_Playlist": "Search Album/Playlist",
    "Drop": "Return to Home",
    "Right_Return_to_Home": "Return to Home",
    "Play / Pause": "Play / Pause",
    "Next Track": "Next Track",
    "Previous Track": "Previous Track",
    "Volume Up": "Volume Up",
    "Volume Down": "Volume Down",
    "Search Album/Playlist": "Search Album/Playlist",
    "Search": "Search Album/Playlist",
    "SEARCH": "Search Album/Playlist",
    "Right_Close_App": "Right_Close_App",
    "Close_App": "Right_Close_App",
    "Close App": "Right_Close_App",
    "Back to Level 2": "Right_Close_App",
    "back_to_level_2": "Right_Close_App",
    "Return to Level 1": "Right_Close_App",
    "return_to_level_1": "Right_Close_App",
    "Back to Level 1": "Right_Close_App",
    "back_to_level_1": "Right_Close_App",
    "Return to Level 2": "Right_Close_App",
    "return_to_level_2": "Right_Close_App",
    "back_to_main_dashboard": "Right_Close_App",
    "Launch JioSaavn": "Launch JioSaavn",
    "Open JioSaavn": "Launch JioSaavn",
}

DISPLAY_LABELS = {
    "Right": "AI/ML Domain Wake",
    "Right_jiosaavn": "Open JioSaavn Web App",
    "Right_Play_Pause": "Play / Pause",
    "Right_Next_Song": "Next Track",
    "Right_Previous_Song": "Previous Track",
    "Right_Volume_Up": "Volume Up (+10%)",
    "Right_Volume_Down": "Volume Down (-10%)",
    "Right_Search_Playlist": "Search & Play Playlist",
    "Drop": "Return to Home",
    "Right_Return_to_Home": "Return to Home"
}

class BCISignalFilter:
    def __init__(self, threshold=CONFIDENCE_THRESHOLD, volume_cooldown=2.0):
        self.threshold = threshold
        self.previous_action = "START"
        self.last_volume_time = 0.0
        self.volume_cooldown = volume_cooldown

    def set_config(self, threshold=None, volume_cooldown=None):
        if threshold is not None:
            self.threshold = float(threshold)
        if volume_cooldown is not None:
            self.volume_cooldown = float(volume_cooldown)

    def evaluate(self, current_timestamp, command, confidence):
        if confidence < self.threshold:
            return "None"
        action = COMMAND_MAPPING.get(command, "None")
        if action == "None":
            return "None"

        # Allow volume repeats only after cooldown
        if action == self.previous_action:
            if action in ["Volume Up", "Volume Down"]:
                if (current_timestamp - self.last_volume_time) >= self.volume_cooldown:
                    self.last_volume_time = current_timestamp
                    return action
            return "None"

        self.previous_action = action
        if action in ["Volume Up", "Volume Down"]:
            self.last_volume_time = current_timestamp
        return action

class BCIPipelineProcessor:
    MODULE_NAME = "BCIPipeline"

    def __init__(self, controller_facade):
        self.facade = controller_facade
        self.bci_filter = BCISignalFilter()
        self.socket_emitter = None
        self._is_running = False
        self._thread = None
        self._lock = threading.Lock()

    def set_socket_emitter(self, emitter_fn):
        self.socket_emitter = emitter_fn

    def emit_event(self, event_name, data):
        if self.socket_emitter:
            try:
                self.socket_emitter(event_name, data)
            except Exception as e:
                logger.debug(self.MODULE_NAME, f"Socket emit error: {e}")

    def is_running(self):
        with self._lock:
            return self._is_running

    def start_automation(self):
        with self._lock:
            if self._is_running:
                logger.info(self.MODULE_NAME, "Automation pipeline is already running.")
                return True
            self._is_running = True

        logger.success(self.MODULE_NAME, "🚀 Starting AI/ML BCI Automation Infinite Loop...")
        self.emit_event('automation_update', {"is_running": True, "status": "RUNNING"})
        
        self._thread = threading.Thread(target=self._automation_loop, daemon=True)
        self._thread.start()
        return True

    def stop_automation(self):
        with self._lock:
            if not self._is_running:
                return False
            self._is_running = False

        logger.info(self.MODULE_NAME, "🛑 Halting AI/ML BCI Automation Infinite Loop...")
        self.emit_event('automation_update', {"is_running": False, "status": "HALTED"})
        return True

    def toggle_automation(self):
        if self.is_running():
            self.stop_automation()
            return False
        else:
            self.start_automation()
            return True

    def run_pipeline(self):
        """Single-pass pipeline alias (backward compatibility)."""
        return self.start_automation()

    def interruptible_sleep(self, seconds: float):
        """Sleeps in small slices so stopping is instantaneous."""
        steps = int(seconds * 10)
        for _ in range(steps):
            if not self._is_running:
                return True
            time.sleep(0.1)
        return False

    def _automation_loop(self):
        """Infinite loop iterating through data/integrated_eeg.json considering only AI/ML domain."""
        json_path = DATA_FILE_PATH if DATA_FILE_PATH.exists() else (Path(__file__).parent / "integrated_eeg.json")
        logger.info(self.MODULE_NAME, f"Loading AI/ML BCI dataset from: {json_path}")

        song_index = 0
        loop_pass = 1

        while self._is_running:
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
            except Exception as e:
                logger.error(self.MODULE_NAME, f"Failed to read dataset file '{json_path}': {e}")
                if self.interruptible_sleep(3.0):
                    break
                continue

            entries = raw_data.get("data", []) if isinstance(raw_data, dict) else (raw_data if isinstance(raw_data, list) else [])
            if not entries:
                logger.warn(self.MODULE_NAME, "No data entries found in BCI dataset.")
                if self.interruptible_sleep(3.0):
                    break
                continue

            config = self.facade.get_config()
            self.bci_filter.set_config(config.get("confidence_threshold"), config.get("volume_cooldown"))

            logger.info(self.MODULE_NAME, f"🔄 [Loop Pass #{loop_pass}] Processing AI/ML stream packets ({len(entries)} total samples)...")

            last_processed_timestamp = 0.0

            for idx, entry in enumerate(entries):
                if not self._is_running:
                    break

                domain = entry.get("domain")
                # STRICT FILTER: Only consider AI/ML domain
                if domain != TARGET_DOMAIN:
                    continue

                raw_command = entry.get("command", "Neutral")
                confidence = float(entry.get("confidence", 0.0))
                timestamp = float(entry.get("timestamp", time.time()))

                # Avoid duplicate timestamp processing within same pass
                if timestamp <= last_processed_timestamp:
                    continue

                # Threshold check
                if confidence < self.bci_filter.threshold:
                    last_processed_timestamp = timestamp
                    continue

                display_label = DISPLAY_LABELS.get(raw_command, raw_command)
                action_to_trigger = COMMAND_MAPPING.get(raw_command)

                if not action_to_trigger:
                    last_processed_timestamp = timestamp
                    continue

                logger.success(self.MODULE_NAME, f"🧠 [AI/ML Sample #{idx}] Command: '{raw_command}' -> Action: '{action_to_trigger}' (Confidence: {confidence * 100:.1f}%)")

                # Broadcast live command dispatch event to Web Dashboard
                self.emit_event('command_dispatched', {
                    "command": raw_command,
                    "display_label": display_label,
                    "action": action_to_trigger,
                    "confidence": confidence,
                    "timestamp": timestamp,
                    "domain": TARGET_DOMAIN,
                    "sample": entry.get("sample", idx)
                })

                # Execute action in JioSaavn
                if action_to_trigger == "Search Album/Playlist":
                    target_song = SONG_QUEUE[song_index % len(SONG_QUEUE)]
                    logger.info(self.MODULE_NAME, f"BCI Trigger: Searching queue item #{song_index + 1}: '{target_song}'")
                    self.facade.execute_action("Search Album/Playlist", query=target_song)
                    song_index += 1
                elif action_to_trigger in ["Open JioSaavn", "Launch JioSaavn"]:
                    self.facade.execute_action("Launch JioSaavn")
                elif action_to_trigger == "Select AI/ML Domain":
                    logger.info(self.MODULE_NAME, "AI/ML Domain Wake Signal Acknowledged.")
                else:
                    self.facade.execute_action(action_to_trigger)

                last_processed_timestamp = timestamp

                # 3-second delay between commands
                if self.interruptible_sleep(3.0):
                    break

            loop_pass += 1
            if self._is_running:
                logger.info(self.MODULE_NAME, "✅ Completed AI/ML dataset pass. Looping again indefinitely in 2s...")
                self.interruptible_sleep(2.0)

        logger.info(self.MODULE_NAME, "AI/ML BCI Automation loop terminated.")
