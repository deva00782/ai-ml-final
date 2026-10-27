import time
import threading
import asyncio
from logger import logger
from browser_manager import BrowserManager
from player_engine import PlayerEngine
from bci_pipeline import BCIPipelineProcessor, CONFIDENCE_THRESHOLD, COMMAND_MAPPING, SONG_QUEUE
from os_operations import get_system_master_volume, set_system_master_volume, get_media_status

class JioSaavnController:
    MODULE_NAME = "JioSaavnController"

    def __init__(self):
        self.lock = threading.RLock()
        self.current_song_title = "Waiting for Track..."
        self.current_song_artist = "Unknown Artist"
        self.is_playing = False
        self.current_volume = float(get_system_master_volume())
        self._manual_song_idx = 0
        
        # Configuration Settings
        self.confidence_threshold = CONFIDENCE_THRESHOLD
        self.volume_cooldown = 3.0
        self.headless = False

        # Modular Engine Components
        self.browser_manager = BrowserManager(headless=self.headless)
        self.player_engine = PlayerEngine(self.browser_manager)
        self.bci_processor = BCIPipelineProcessor(self)

        # Start Media Status Poller thread
        threading.Thread(target=self._start_async_poll, daemon=True).start()

    def get_config(self):
        with self.lock:
            return {
                "confidence_threshold": self.confidence_threshold,
                "volume_cooldown": self.volume_cooldown,
                "headless": self.headless
            }

    def update_config(self, confidence_threshold=None, volume_cooldown=None, headless=None):
        with self.lock:
            restart_needed = False
            if confidence_threshold is not None:
                self.confidence_threshold = float(confidence_threshold)
            if volume_cooldown is not None:
                self.volume_cooldown = float(volume_cooldown)
            if headless is not None:
                new_h = bool(headless)
                if new_h != self.headless:
                    self.headless = new_h
                    self.browser_manager.headless = new_h
                    restart_needed = True

            if restart_needed:
                logger.info(self.MODULE_NAME, f"Headless mode updated to {self.headless}. Restarting browser engine...")
                self.browser_manager.quit_driver()

            logger.info(self.MODULE_NAME, f"Config updated: Threshold={self.confidence_threshold}, Cooldown={self.volume_cooldown}s, Headless={self.headless}")
            return {
                "confidence_threshold": self.confidence_threshold,
                "volume_cooldown": self.volume_cooldown,
                "headless": self.headless
            }

    def _start_async_poll(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._poll_status())

    async def _poll_status(self):
        import platform
        logger.info(self.MODULE_NAME, f"Starting media status polling for {platform.system()}")

        while True:
            await asyncio.sleep(1.0)
            try:
                # Live poll system master volume (Always runs)
                sys_vol = get_system_master_volume()

                # Get media status using cross-platform function
                media_status = await get_media_status()

                with self.lock:
                    self.current_song_title = media_status.get("title", "Waiting for Track...")
                    self.current_song_artist = media_status.get("artist", "Unknown Artist")
                    self.is_playing = media_status.get("is_playing", False)
                    self.current_volume = float(sys_vol)
            except Exception as e:
                logger.warn(self.MODULE_NAME, f"Error during status polling: {e}")
                # Fallback to volume-only polling
                try:
                    sys_vol = get_system_master_volume()
                    with self.lock:
                        self.current_volume = float(sys_vol)
                except Exception:
                    pass

    def get_status(self):
        return {
            "title": self.current_song_title,
            "artist": self.current_song_artist,
            "is_playing": self.is_playing,
            "volume": self.current_volume,
            "automation_active": self.bci_processor.is_running()
        }

    def toggle_automation(self):
        return self.bci_processor.toggle_automation()

    def is_automation_active(self):
        return self.bci_processor.is_running()

    def close_browser(self):
        with self.lock:
            self.is_playing = False
            self.current_song_title = "Waiting for Track..."
            self.current_song_artist = "Unknown Artist"
            return self.browser_manager.close_browser()

    def execute_action(self, action, query=None):
        with self.lock:
            normalized_action = COMMAND_MAPPING.get(action, action)
            if normalized_action in ["Open JioSaavn", "Launch JioSaavn"]:
                normalized_action = "Launch JioSaavn"
            elif normalized_action in ["Search", "SEARCH", "search"]:
                normalized_action = "Search Album/Playlist"

            logger.info(self.MODULE_NAME, f"Received action execution request: '{action}' (Normalized: '{normalized_action}', Query: {query})")
            
            if normalized_action in ["Run BCI Automation", "Toggle Automation", "Start Automation"]:
                if action == "Start Automation":
                    self.bci_processor.start_automation()
                    return {"action": "success", "message": "Automation Started", "automation_active": True}
                elif action == "Stop Automation":
                    self.bci_processor.stop_automation()
                    return {"action": "success", "message": "Automation Stopped", "automation_active": False}
                else:
                    is_active = self.bci_processor.toggle_automation()
                    state_msg = "Started" if is_active else "Stopped"
                    return {"action": "success", "message": f"Automation {state_msg}", "automation_active": is_active}

            elif normalized_action == "Stop Automation":
                self.bci_processor.stop_automation()
                return {"action": "success", "message": "Automation Stopped", "automation_active": False}

            elif str(normalized_action).strip().lower() in [
                "close app", "close_app", "right_close_app",
                "back to level 2", "back_to_level_2",
                "return to level 1", "return_to_level_1",
                "return to level 2", "return_to_level_2",
                "back to level 1", "back_to_level_1",
                "back to main dashboard", "back_to_main_dashboard"
            ]:
                self.close_browser()
                return {"action": "success", "message": "Closed JioSaavn Chrome on exit"}

            elif normalized_action == "Launch JioSaavn":
                self.browser_manager.open_new_tab("https://www.jiosaavn.com")
                return {"action": "success", "message": "Opened JioSaavn in new tab"}

            elif normalized_action == "Search Album/Playlist":
                target_query = query if query else (SONG_QUEUE[self._manual_song_idx % len(SONG_QUEUE)])
                self._manual_song_idx += 1
                logger.info(self.MODULE_NAME, f"Executing Search & Play for '{target_query}'...")
                success = self.player_engine.search_and_play(target_query)
                status = "success" if success else "failed"
                return {"action": status, "message": f"Searched '{target_query}' ({status})"}

            elif normalized_action == "Play / Pause":
                success = self.player_engine.play_pause()
                return f"Executed Play / Pause ({'Success' if success else 'Failed'})"

            elif normalized_action == "Next Track":
                success = self.player_engine.next_track()
                return f"Executed Next Track ({'Success' if success else 'Failed'})"

            elif normalized_action == "Previous Track":
                success = self.player_engine.previous_track()
                return f"Executed Previous Track ({'Success' if success else 'Failed'})"

            elif normalized_action in ["Volume Up", "Volume Down"]:
                success = self.player_engine.adjust_volume(normalized_action)
                sys_vol = get_system_master_volume()
                self.current_volume = float(sys_vol)
                result = "Success" if success else "Failed"
                logger.info(self.MODULE_NAME, f"Executed {normalized_action} -> Master Volume: {sys_vol}% ({result})")
                return f"Executed {normalized_action} (Volume: {sys_vol}% - {result})"

            elif normalized_action == "Return to Home":
                self.browser_manager.focus_or_open_tab("https://www.jiosaavn.com/")
                return {"action": "success", "message": "Returned Home"}

            elif normalized_action == "Select AI/ML Domain":
                logger.info(self.MODULE_NAME, "AI/ML Domain Wake Signal Acknowledged.")
                return {"action": "success", "message": "AI/ML Domain Acknowledged"}

        return f"Unknown action: {action}"