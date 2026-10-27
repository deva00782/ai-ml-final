import json
from agents.logger import get_logger

logger = get_logger("parser")

SEARCH_PREFIXES = ("search|", "playlist|", "artist|", "singer|", "actor|", "album|", "song|")
SEARCH_KEYWORDS = {"search", "playlist", "artist", "singer", "actor", "album", "song"}

DIRECT_COMMAND_MAP = {
    "open": "open_desktop_dashboard",
    "open_jiosaavn": "open_desktop_dashboard",
    "open_desktop_dashboard": "open_desktop_dashboard",
    "play": "play_pause",
    "pause": "play_pause",
    "play_pause": "play_pause",
    "media_play_pause": "play_pause",
    "next": "next_track",
    "media_next": "next_track",
    "next_track": "next_track",
    "playnext": "next_track",
    "play_next": "next_track",
    "nextsong": "next_track",
    "next_song": "next_track",
    "prev": "previous_track",
    "previous": "previous_track",
    "media_previous": "previous_track",
    "previous_track": "previous_track",
    "playprev": "previous_track",
    "play_prev": "previous_track",
    "volume_up": "volume_up",
    "volume up": "volume_up",
    "vol_up": "volume_up",
    "vol up": "volume_up",
    "vol+": "volume_up",
    "volume+": "volume_up",
    "media_volume_up": "volume_up",
    "volume_down": "volume_down",
    "volume down": "volume_down",
    "vol_down": "volume_down",
    "vol down": "volume_down",
    "vol-": "volume_down",
    "volume-": "volume_down",
    "media_volume_down": "volume_down",
    "play / pause": "play_pause",
    "play/pause": "play_pause",
    "next track": "next_track",
    "previous track": "previous_track",
    "search": "search",
    "playlist": "search",
    "artist": "search",
    "singer": "search",
    "actor": "search",
    "album": "search",
    "back_to_level_2": "back_to_level_2",
    "back to level 2": "back_to_level_2",
    "push+right": "back_to_level_2",
    "push + right": "back_to_level_2",
    "back_to_level_1": "back_to_level_1",
    "back to level 1": "back_to_level_1",
    "right_close_app": "back_to_level_2",
    "close_app": "back_to_level_2",
    "close app": "back_to_level_2",
}

JSON_COMMAND_MAP = {
    "PING": ("PING", None),
    "DASHBOARD_OPEN": ("open_desktop_dashboard", None),
    "OPEN_JIOSAAVN": ("open_desktop_dashboard", None),
    "OPEN": ("open_desktop_dashboard", None),
    "NEXT": ("next_track", None),
    "NEXT_TRACK": ("next_track", None),
    "PLAYNEXT": ("next_track", None),
    "PLAY_NEXT": ("next_track", None),
    "NEXT_SONG": ("next_track", None),
    "NEXTSONG": ("next_track", None),
    "PREV": ("previous_track", None),
    "PREVIOUS": ("previous_track", None),
    "PREVIOUS_TRACK": ("previous_track", None),
    "PLAYPREV": ("previous_track", None),
    "PLAY_PREV": ("previous_track", None),
    "RUN_TASK": ("play_pause", None),
    "PLAY_PAUSE": ("play_pause", None),
    "PLAY": ("play_pause", None),
    "PAUSE": ("play_pause", None),
    "STOP_TASK": ("play_pause", None),
    "VOLUME_UP": ("volume_up", None),
    "VOLUME UP": ("volume_up", None),
    "VOL_UP": ("volume_up", None),
    "VOL UP": ("volume_up", None),
    "VOLUME_DOWN": ("volume_down", None),
    "VOLUME DOWN": ("volume_down", None),
    "VOL_DOWN": ("volume_down", None),
    "VOL DOWN": ("volume_down", None),
    "BACK_TO_LEVEL_2": ("back_to_level_2", None),
    "RIGHT_CLOSE_APP": ("back_to_level_2", None),
    "PUSH+RIGHT": ("back_to_level_2", None),
    "CLOSE_APP": ("back_to_level_2", None),
}


class CommandHandler:
    @staticmethod
    def _strip_prefix(raw_data: str) -> str:
        cleaned = raw_data.strip()
        if cleaned.startswith("action cmd|"):
            return cleaned[len("action cmd|"):]
        if cleaned.startswith("cmd|"):
            return cleaned[len("cmd|"):]
        return cleaned

    @staticmethod
    def _match_search_entity(cleaned: str):
        cleaned_lower = cleaned.lower()
        if cleaned_lower in SEARCH_KEYWORDS:
            return "search", ""
        for pfx in SEARCH_PREFIXES:
            if cleaned_lower.startswith(pfx):
                parts = cleaned.split("|", 1)
                query = parts[1].strip() if len(parts) > 1 else ""
                logger.debug(f"Parsed raw entity command: {pfx[:-1].upper()} -> search (query: {query})")
                return "search", query
        return None

    @staticmethod
    def _match_direct_command(cleaned: str):
        target_cmd = DIRECT_COMMAND_MAP.get(cleaned.lower())
        if target_cmd:
            logger.debug(f"Parsed valid direct command: {cleaned} -> {target_cmd}")
            return target_cmd, None
        return None

    @staticmethod
    def _extract_query_from_payload(payload) -> str:
        if isinstance(payload, dict):
            for k in ("query", "song", "title", "playlist", "artist", "singer", "actor", "album", "name"):
                if payload.get(k):
                    return str(payload[k]).strip()
        elif payload:
            return str(payload).strip()
        return ""

    @classmethod
    def _parse_json_payload(cls, cleaned: str):
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return None

        cmd = data.get("command", "")
        cmd_upper = cmd.upper()
        payload = data.get("payload", {}) or {}

        if cmd_upper in ("SEARCH", "PLAYLIST", "ARTIST", "SINGER", "ACTOR", "ALBUM", "SONG"):
            query = cls._extract_query_from_payload(payload)
            logger.debug(f"Parsed JSON entity command: {cmd_upper} -> search (query: {query})")
            return "search", query

        mapped = JSON_COMMAND_MAP.get(cmd_upper)
        if mapped:
            logger.debug(f"Parsed JSON command: {cmd} -> {mapped[0]}")
            return mapped

        return cmd.lower(), None

    @staticmethod
    def _parse_fallback(cleaned: str):
        cleaned_upper = cleaned.upper()
        if "PING" in cleaned_upper:
            return "PING", None
        if "NEXT" in cleaned_upper:
            return "next_track", None
        if "PREV" in cleaned_upper:
            return "previous_track", None
        if "PLAY" in cleaned_upper or "PAUSE" in cleaned_upper:
            return "play_pause", None
        if "VOL" in cleaned_upper and "UP" in cleaned_upper or cleaned_upper in ("VOLUME+", "VOL+"):
            return "volume_up", None
        if "VOL" in cleaned_upper and "DOWN" in cleaned_upper or cleaned_upper in ("VOLUME-", "VOL-"):
            return "volume_down", None
        if "OPEN" in cleaned_upper:
            return "open_desktop_dashboard", None
        if "BACK_TO_LEVEL_2" in cleaned_upper or "PUSH+RIGHT" in cleaned_upper or "RIGHT_CLOSE_APP" in cleaned_upper or "BACK TO LEVEL 2" in cleaned_upper:
            return "back_to_level_2", None
        for entity_keyword in ("SEARCH", "PLAYLIST", "ARTIST", "SINGER", "ACTOR", "ALBUM", "SONG"):
            if entity_keyword in cleaned_upper:
                parts = cleaned.split("|", 1)
                query = parts[1].strip() if len(parts) > 1 else ""
                return "search", query
        return None

    @classmethod
    def parse(cls, raw_data):
        if not raw_data:
            logger.warning("Received empty or whitespace-only raw data")
            return "UNKNOWN", None

        cleaned = cls._strip_prefix(raw_data)

        if cleaned.upper() == "LOOP":
            logger.debug("Parsed raw legacy command: LOOP")
            return "LOOP", None

        search_res = cls._match_search_entity(cleaned)
        if search_res:
            return search_res

        direct_res = cls._match_direct_command(cleaned)
        if direct_res:
            return direct_res

        json_res = cls._parse_json_payload(cleaned)
        if json_res:
            return json_res

        fallback_res = cls._parse_fallback(cleaned)
        if fallback_res:
            return fallback_res

        logger.warning(f"Unrecognized incoming raw command: {raw_data!r}")
        return "UNKNOWN", None