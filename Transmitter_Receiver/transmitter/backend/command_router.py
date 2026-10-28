"""Unified command router with PostgreSQL persistence.

Single-target dispatch (``mobile`` | ``desktop`` | ``ios`` | ``uart``) reuses each
dashboard's existing control path:

    {"command": "NEXT_TRACK", "target": "mobile"}   -> MQTT publish (recorded)
    {"command": "NEXT_TRACK", "target": "ios"}      -> MQTT iOS topic (recorded)
    {"command": "NEXT_TRACK", "target": "uart"}     -> UART transmit (recorded)
    {"command": "NEXT_TRACK", "target": "desktop"}  -> UART forward to receiver (recorded)

Only remote-targeted commands (mobile, ios, uart, desktop) are persisted to
PostgreSQL command_history. Local desktop execution is never recorded.
"""

from typing import Optional
import time
import uuid
from .device_manager import device_manager

try:
    from .postgres_store import get_postgres_store
except ImportError:
    get_postgres_store = None


SEARCH_COMMAND_ALIASES = (
    "SEARCH", "Right_Search_Playlist", "Search Album/Playlist",
    "Search", "RIGHT", "search", "search_album_playlist",
)


class CommandRouter:
    def __init__(self):
        # Mobile play/pause is a single toggle: guard rapid duplicates so only
        # ONE state change (play OR pause) reaches the APK per intent.
        self._last_mobile_publish = {"command": None, "ts": 0.0}
        self._pg_store = None

    def _get_pg_store(self):
        """Lazy-load postgres store to avoid circular imports."""
        if self._pg_store is None and get_postgres_store is not None:
            self._pg_store = get_postgres_store()
        return self._pg_store

    def _is_remote_target(self, target: str) -> bool:
        """Check if target is a remote device (not local desktop execution)."""
        return target.lower() in ("mobile", "ios", "uart", "desktop")

    def _get_target_device_id(self, target: str, mqtt_service=None) -> str:
        """Get the actual device identifier for the target."""
        if target in ("mobile", "ios") and mqtt_service:
            return getattr(mqtt_service, "active_device", "unknown") or "unknown"
        if target in ("uart", "desktop"):
            return "uart"  # Use target type for consistency
        return "unknown"

    def _record_command_received(
        self,
        command: str,
        transport: str,
        target: str,
        mqtt_service=None,
        query: str = None,
        song_url: str = None,
        title: str = None,
        payload: dict = None,
    ) -> Optional[int]:
        """Record command as received. Returns command_history ID or None."""
        pg = self._get_pg_store()
        if not pg:
            return None

        # Pass target type (mobile/ios/uart/desktop) for persistence decision
        # The actual device ID is stored in payload for debugging

        # Generate command_id for correlation
        command_id = str(uuid.uuid4())[:8]

        # Build payload for debugging
        target_device_id = self._get_target_device_id(target, mqtt_service)
        debug_payload = {
            "command": command,
            "target": target,
            "target_device_id": target_device_id,
            "query": query,
            "song_url": song_url,
            "title": title,
        }
        if payload:
            debug_payload.update(payload)

        return pg.record_command_received(
            command=command,
            transport=transport,
            target_device=target,  # Pass target type for remote check
            command_id=command_id,
            query=query,
            song_url=song_url,
            title=title,
            payload=debug_payload,
        )

    def _update_command_published(self, cmd_history_id: int) -> bool:
        """Update command status to published."""
        pg = self._get_pg_store()
        if not pg or not cmd_history_id:
            return False
        return pg.update_command_status(cmd_history_id, "published")

    def _update_command_failed(self, cmd_history_id: int, reason: str) -> bool:
        """Update command status to failed."""
        pg = self._get_pg_store()
        if not pg or not cmd_history_id:
            return False
        return pg.update_command_status(cmd_history_id, "failed", reason)

    def update_command_by_correlation(
        self,
        command: str,
        transport: str,
        target_device: str,
        status: str,
        command_id: str = None,
        error_message: str = None,
    ) -> bool:
        """Update command status using correlation fields (for ACK handling)."""
        pg = self._get_pg_store()
        if not pg:
            return False
        return pg.update_command_by_correlation(
            command=command,
            transport=transport,
            target_device=target_device,
            status=status,
            command_id=command_id,
            error_message=error_message,
        )

    def _route_uart(self, command, query):
        cmd_history_id = None
        try:
            try:
                from .uart_transmitter import uart_transmitter
            except ImportError:
                from uart_transmitter import uart_transmitter
            if uart_transmitter and uart_transmitter.enabled:
                if command in SEARCH_COMMAND_ALIASES:
                    effective_query = (query or "").strip() if (query and str(query).strip()) else "Inaam"
                    cmd_history_id = self._record_command_received(
                        command, "uart", "uart", query=effective_query, song_url=effective_query, title=effective_query
                    )
                    uart_transmitter.send_search(effective_query)
                    if cmd_history_id:
                        self._update_command_published(cmd_history_id)
                    return {"command": command, "target": "uart", "dispatched": True, "channel": "uart", "query": effective_query, "cmd_history_id": cmd_history_id}
                cmd_history_id = self._record_command_received(command, "uart", "uart")
                uart_transmitter.send_action(command)
                if cmd_history_id:
                    self._update_command_published(cmd_history_id)
                return {"command": command, "target": "uart", "dispatched": True, "channel": "uart", "cmd_history_id": cmd_history_id}
            return {"command": command, "target": "uart", "dispatched": False, "reason": "UART disabled"}
        except Exception as e:
            if cmd_history_id:
                self._update_command_failed(cmd_history_id, str(e))
            return {"command": command, "target": "uart", "dispatched": False, "reason": str(e)}

    def _route_mobile(self, command, target, query):
        mqtt = device_manager.get_mobile().get("mqtt_service")
        if mqtt is None:
            return {
                "command": command,
                "target": target,
                "dispatched": False,
                "reason": "mobile backend not registered",
            }
        client = getattr(mqtt, "client", None)
        if client is None or not client.is_connected():
            return {
                "command": command,
                "target": target,
                "dispatched": False,
                "reason": "MQTT not connected (broker unreachable) - APK will not receive the command",
            }

        now = time.time()
        if command == "Right_Play_Pause":
            if self._last_mobile_publish["command"] == command and (now - self._last_mobile_publish["ts"]) < 1.0:
                return {
                    "command": command,
                    "target": target,
                    "dispatched": True,
                    "channel": "mqtt",
                    "reason": "duplicate play/pause suppressed within 1s (single action only)",
                }
            self._last_mobile_publish = {"command": command, "ts": now}

        if command in SEARCH_COMMAND_ALIASES:
            effective_query = (query or "").strip() if (query and str(query).strip()) else "Inaam"
            cmd_history_id = None
            try:
                cmd_history_id = self._record_command_received(
                    command, "mqtt", target, mqtt, query=effective_query
                )
                search_res = mqtt.publish_search_command(effective_query, target=target)
                song_url = (search_res or {}).get("song_url", "")
                song_title = (search_res or {}).get("title", effective_query)
                if cmd_history_id:
                    pg = self._get_pg_store()
                    if pg:
                        with pg._get_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    "UPDATE command_history SET song_url = %s, title = %s, status = 'published', updated_at = NOW() WHERE id = %s",
                                    (song_url, song_title, cmd_history_id),
                                )
                            conn.commit()
                return {
                    "command": command,
                    "target": target,
                    "dispatched": True,
                    "channel": "mqtt",
                    "query": effective_query,
                    "song_url": song_url,
                    "title": song_title,
                    "cmd_history_id": cmd_history_id,
                }
            except Exception as e:
                if cmd_history_id:
                    self._update_command_failed(cmd_history_id, str(e))

        cmd_history_id = None
        try:
            if command != "SEARCH":
                cmd_history_id = self._record_command_received(command, "mqtt", target, mqtt)
            mqtt.publish_command(command, 0.99, now, target=target)
            if cmd_history_id:
                self._update_command_published(cmd_history_id)
        except TypeError:
            mqtt.publish_command(command, 0.99, now)
            if cmd_history_id:
                self._update_command_published(cmd_history_id)
        return {"command": command, "target": target, "dispatched": True, "channel": "mqtt", "cmd_history_id": cmd_history_id}

    def _route_desktop_uart(self, command, query, target, uart_transmitter):
        if command in SEARCH_COMMAND_ALIASES:
            effective_query = (query or "").strip() if (query and str(query).strip()) else "Inaam"
            cmd_history_id = None
            try:
                cmd_history_id = self._record_command_received(
                    command, "uart", "desktop", query=effective_query, song_url=effective_query, title=effective_query
                )
                uart_transmitter.send_search(effective_query)
                if cmd_history_id:
                    self._update_command_published(cmd_history_id)
                return {
                    "command": command,
                    "target": target,
                    "dispatched": True,
                    "channel": "uart",
                    "query": effective_query,
                    "result": f"UART forwarded: search|{effective_query}",
                    "cmd_history_id": cmd_history_id,
                }
            except Exception as e:
                if cmd_history_id:
                    self._update_command_failed(cmd_history_id, f"UART search forward failed: {e}")
                return {
                    "command": command,
                    "target": target,
                    "dispatched": False,
                    "reason": f"UART search forward failed: {e}",
                }

        cmd_history_id = self._record_command_received(command, "uart", "desktop")
        uart_transmitter.send_action(command)
        return {
            "command": command,
            "target": target,
            "dispatched": True,
            "channel": "uart",
            "result": f"UART forwarded: {command}",
            "cmd_history_id": cmd_history_id,
        }

    def _route_desktop_local(self, command, query, target):
        controller = device_manager.get_desktop().get("controller")
        if controller is None:
            return {
                "command": command,
                "target": target,
                "dispatched": False,
                "reason": "desktop backend not registered",
            }

        cmd_history_id = None
        try:
            cmd_history_id = self._record_command_received(command, "local", "desktop", query=query)
            result = controller.execute_action(command, query=query)
            if cmd_history_id:
                self._update_command_published(cmd_history_id)
            return {
                "command": command,
                "target": target,
                "dispatched": True,
                "channel": "local_desktop",
                "result": str(result),
                "cmd_history_id": cmd_history_id,
            }
        except Exception as e:
            if cmd_history_id:
                self._update_command_failed(cmd_history_id, str(e))
            return {
                "command": command,
                "target": target,
                "dispatched": False,
                "reason": f"Local desktop execution error: {e}",
            }

    def _route_desktop(self, command, query, target):
        try:
            from .uart_transmitter import uart_transmitter
        except ImportError:
            from uart_transmitter import uart_transmitter

        uart_connected = bool(uart_transmitter and uart_transmitter.enabled and uart_transmitter.is_connected())
        if uart_connected:
            return self._route_desktop_uart(command, query, target, uart_transmitter)
        return self._route_desktop_local(command, query, target)

    def route(self, command, target="mobile", query=None):
        if target not in ("mobile", "desktop", "ios", "uart"):
            raise ValueError(f"Unknown target: {target} (expected 'mobile', 'desktop', 'ios', or 'uart')")

        if target == "uart":
            return self._route_uart(command, query)
        if target in ("mobile", "ios"):
            return self._route_mobile(command, target, query)
        return self._route_desktop(command, query, target)

    def route_multi(self, command, targets=("mobile", "desktop")):
        """Future-proof multi-target dispatch. ``'both'`` is not supported yet."""
        if targets == "both":
            raise NotImplementedError(
                "Broadcast-to-both is not implemented yet; use ['mobile'] or ['desktop']."
            )
        return [self.route(command, target=t) for t in targets]


command_router = CommandRouter()