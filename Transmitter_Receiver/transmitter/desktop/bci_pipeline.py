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

CMD_OPEN_JIOSAAVN = "Open JioSaavn"
CMD_PLAY_PAUSE = "Play / Pause"
CMD_NEXT_TRACK = "Next Track"
CMD_PREV_TRACK = "Previous Track"
CMD_VOLUME_UP = "Volume Up"
CMD_VOLUME_DOWN = "Volume Down"
CMD_SEARCH_PLAYLIST = "Search Album/Playlist"
CMD_RETURN_HOME = "Return to Home"
CMD_LAUNCH_JIOSAAVN = "Launch JioSaavn"

COMMAND_MAPPING = {
    "Right": "Select AI/ML Domain",
    "Right_jiosaavn": CMD_OPEN_JIOSAAVN,
    "Right_Play_Pause": CMD_PLAY_PAUSE,
    "Right_Next_Song": CMD_NEXT_TRACK,
    "Right_Previous_Song": CMD_PREV_TRACK,
    "Right_Volume_Up": CMD_VOLUME_UP,
    "Right_Volume_Down": CMD_VOLUME_DOWN,
    "Right_Search_Playlist": CMD_SEARCH_PLAYLIST,
    "Drop": CMD_RETURN_HOME,
    "Right_Return_to_Home": CMD_RETURN_HOME,
    "Play / Pause": CMD_PLAY_PAUSE,
    "Next Track": CMD_NEXT_TRACK,
    "Previous Track": CMD_PREV_TRACK,
    "Volume Up": CMD_VOLUME_UP,
    "Volume Down": CMD_VOLUME_DOWN,
    "Search Album/Playlist": CMD_SEARCH_PLAYLIST,
    "Search": CMD_SEARCH_PLAYLIST,
    "SEARCH": CMD_SEARCH_PLAYLIST,
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
    "Launch JioSaavn": CMD_LAUNCH_JIOSAAVN,
    "Open JioSaavn": CMD_LAUNCH_JIOSAAVN,
}

DISPLAY_LABELS = {
    "Right": "AI/ML Domain Wake",
    "Right_jiosaavn": "Open JioSaavn Web App",
    "Right_Play_Pause": CMD_PLAY_PAUSE,
    "Right_Next_Song": CMD_NEXT_TRACK,
    "Right_Previous_Song": CMD_PREV_TRACK,
    "Right_Volume_Up": "Volume Up (+10%)",
    "Right_Volume_Down": "Volume Down (-10%)",
    "Right_Search_Playlist": "Search & Play Playlist",
    "Drop": CMD_RETURN_HOME,
    "Right_Return_to_Home": CMD_RETURN_HOME
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
            if action in [CMD_VOLUME_UP, CMD_VOLUME_DOWN]:
                if (current_timestamp - self.last_volume_time) >= self.volume_cooldown:
                    self.last_volume_time = current_timestamp
                    return action
            return "None"

        self.previous_action = action
        if action in [CMD_VOLUME_UP, CMD_VOLUME_DOWN]:
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
                return False
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

    def _load_dataset(self, json_path: Path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            if isinstance(raw_data, dict):
                return raw_data.get("data", [])
            if isinstance(raw_data, list):
                return raw_data
            return []
        except Exception as e:
            logger.error(self.MODULE_NAME, f"Failed to read dataset file '{json_path}': {e}")
            return None

    def _execute_bci_action(self, action_to_trigger: str, song_index: int) -> int:
        if action_to_trigger == CMD_SEARCH_PLAYLIST:
            target_song = SONG_QUEUE[song_index % len(SONG_QUEUE)]
            logger.info(self.MODULE_NAME, f"BCI Trigger: Searching queue item #{song_index + 1}: '{target_song}'")
            self.facade.execute_action(CMD_SEARCH_PLAYLIST, query=target_song)
            return song_index + 1
        elif action_to_trigger in [CMD_OPEN_JIOSAAVN, CMD_LAUNCH_JIOSAAVN]:
            self.facade.execute_action(CMD_LAUNCH_JIOSAAVN)
        elif action_to_trigger == "Select AI/ML Domain":
            logger.info(self.MODULE_NAME, "AI/ML Domain Wake Signal Acknowledged.")
        else:
            self.facade.execute_action(action_to_trigger)
        return song_index

    def _process_entry(self, idx, entry, last_processed_timestamp, song_index):
        if entry.get("domain") != TARGET_DOMAIN:
            return last_processed_timestamp, song_index, False

        raw_command = entry.get("command", "Neutral")
        confidence = float(entry.get("confidence", 0.0))
        timestamp = float(entry.get("timestamp", time.time()))

        if timestamp <= last_processed_timestamp or confidence < self.bci_filter.threshold:
            return max(timestamp, last_processed_timestamp), song_index, False

        action_to_trigger = COMMAND_MAPPING.get(raw_command)
        if not action_to_trigger:
            return timestamp, song_index, False

        logger.success(self.MODULE_NAME, f"🧠 [AI/ML Sample #{idx}] Command: '{raw_command}' -> Action: '{action_to_trigger}' (Confidence: {confidence * 100:.1f}%)")

        display_label = DISPLAY_LABELS.get(raw_command, raw_command)
        self.emit_event('command_dispatched', {
            "command": raw_command,
            "display_label": display_label,
            "action": action_to_trigger,
            "confidence": confidence,
            "timestamp": timestamp,
            "domain": TARGET_DOMAIN,
            "sample": entry.get("sample", idx)
        })

        song_index = self._execute_bci_action(action_to_trigger, song_index)
        return timestamp, song_index, True

    def _ensure_entries(self, json_path):
        entries = self._load_dataset(json_path)
        if not entries:
            if entries is not None:
                logger.warn(self.MODULE_NAME, "No data entries found in BCI dataset.")
            return None
        return entries

    def _process_entries(self, entries, song_index):
        last_processed_timestamp = 0.0
        for idx, entry in enumerate(entries):
            if not self._is_running:
                break

            last_processed_timestamp, song_index, executed = self._process_entry(
                idx, entry, last_processed_timestamp, song_index
            )

            if executed and self.interruptible_sleep(3.0):
                break
        return song_index

    def _automation_loop(self):
        """Infinite loop iterating through data/integrated_eeg.json considering only AI/ML domain."""
        json_path = DATA_FILE_PATH if DATA_FILE_PATH.exists() else (Path(__file__).parent / "integrated_eeg.json")
        logger.info(self.MODULE_NAME, f"Loading AI/ML BCI dataset from: {json_path}")

        song_index = 0
        loop_pass = 1

        while self._is_running:
            entries = self._ensure_entries(json_path)
            if entries is None:
                if self.interruptible_sleep(3.0):
                    break
                continue

            config = self.facade.get_config()
            self.bci_filter.set_config(config.get("confidence_threshold"), config.get("volume_cooldown"))

            logger.info(self.MODULE_NAME, f"🔄 [Loop Pass #{loop_pass}] Processing AI/ML stream packets ({len(entries)} total samples)...")
            song_index = self._process_entries(entries, song_index)

            loop_pass += 1
            if self._is_running:
                logger.info(self.MODULE_NAME, "✅ Completed AI/ML dataset pass. Looping again indefinitely in 2s...")
                self.interruptible_sleep(2.0)

        logger.info(self.MODULE_NAME, "AI/ML BCI Automation loop terminated.")
