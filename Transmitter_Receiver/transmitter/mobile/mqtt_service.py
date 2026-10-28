from __future__ import annotations

import os
import json
import logging
import uuid
import sys
import time
import threading
from pathlib import Path
from datetime import datetime

# Add parent directory and repository site-packages to sys.path (Sprint 11 robust fallback retained)
_CURRENT_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _CURRENT_DIR.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# Auto-discover site-packages from receiver/venv if running in an unlinked environment
for _candidate_root in _CURRENT_DIR.parents:
    _site_pkgs = _candidate_root / "receiver" / "venv" / "Lib" / "site-packages"
    if _site_pkgs.is_dir() and str(_site_pkgs) not in sys.path:
        sys.path.insert(0, str(_site_pkgs))
        break

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

# Device Database Service (AWS RDS PostgreSQL)
try:
    from . import device_db_service as db_service
except ImportError:
    try:
        from mobile_dashboard.mobile_plugin.services import device_db_service as db_service
    except ImportError:
        import device_db_service as db_service

# Sync API client
try:
    from . import sync_api_client as sync_client
except ImportError:
    try:
        import sync_api_client as sync_client
    except ImportError:
        sync_client = None

# PostgreSQL store (optional telemetry and command history)
try:
    from backend.postgres_store import get_postgres_store
except ImportError:
    try:
        from transmitter.backend.postgres_store import get_postgres_store
    except ImportError:
        get_postgres_store = None

# Command router for command acknowledgement handling
try:
    from backend.command_router import command_router
except ImportError:
    try:
        from transmitter.backend.command_router import command_router
    except ImportError:
        command_router = None

try:
    from . import config
except ImportError:
    import config
MQTT_BROKER = config.MQTT_BROKER
MQTT_FALLBACK_BROKER = config.MQTT_FALLBACK_BROKER
MQTT_PORT = config.MQTT_PORT
MQTT_QOS = config.MQTT_QOS
MQTT_KEEPALIVE = config.MQTT_KEEPALIVE
CLIENT_ID_PREFIX = config.CLIENT_ID_PREFIX
MQTT_NAMESPACE = config.MQTT_NAMESPACE
TOPIC_DISCOVERY = config.TOPIC_DISCOVERY
get_device_topics = config.get_device_topics
TOPIC_COMMAND = config.TOPIC_COMMAND
TOPIC_IOS_COMMAND = config.TOPIC_IOS_COMMAND
TOPIC_ACK = config.TOPIC_ACK
TOPIC_STATUS = config.TOPIC_STATUS
SERVER_STATUS_TOPIC = config.SERVER_STATUS_TOPIC
TOPIC_HEARTBEAT = config.TOPIC_HEARTBEAT
TOPIC_MEDIA = config.TOPIC_MEDIA
ONLINE_PAYLOAD = config.ONLINE_PAYLOAD
LAST_WILL_PAYLOAD = config.LAST_WILL_PAYLOAD
DEBUG_MODE = config.DEBUG_MODE
COMMAND_MAP = config.COMMAND_MAP
DISPLAY_LABELS = config.DISPLAY_LABELS
TOPIC_REGISTER_RESPONSE = "aiml/devices/register_response"

try:
    from backend.payload_logger import log_mqtt_payload
except ImportError:
    try:
        from transmitter.backend.payload_logger import log_mqtt_payload
    except ImportError:
        def log_mqtt_payload(topic: str, payload: dict, target: str = "mobile"):
            # No-op fallback when payload_logger is not importable
            pass


class MQTTService:
    """MQTT service for handling communication with the BCI mobile and iOS apps."""
    
    def __init__(self, socketio, pipeline_scheduler=None):
        self.socketio = socketio
        self.client = None
        self.logger = logging.getLogger()
        self.pipeline_scheduler = pipeline_scheduler
        self.manual_command_active = False
        self.manual_command_timeout = None
        self.media_subscribed = True  # Track media subscription state
        self.active_broker = MQTT_BROKER

        # Dynamic Device Registry (loaded from AWS RDS PostgreSQL database)
        self.active_device = None
        self.registered_devices = {}
        self.device_online_statuses = {}
        self._load_devices_from_db()

    def _load_devices_from_db(self):
        """Fetch all registered devices from AWS RDS PostgreSQL database on startup."""
        try:
            db_devices = db_service.get_all_devices()
            for dev in db_devices:
                dev_id = dev.get("device_id")
                uname = (dev.get("username") or "").strip().lower()
                dev_type = dev.get("device_type") or "android"
                if dev_id and uname:
                    self.registered_devices[dev_id] = {
                        "device_id": dev_id,
                        "username": uname,
                        "device_type": dev_type
                    }
            if self.registered_devices and not self.active_device:
                first_dev = next(iter(self.registered_devices.values()))
                self.active_device = first_dev.get("username") or first_dev.get("device_id")
            self.logger.info(f"[POSTGRES] Loaded {len(self.registered_devices)} devices from database. Active: {self.active_device}")
        except Exception as e:
            self.logger.warning(f"[POSTGRES] Could not load devices from database: {e}")

    def _get_active_identifiers(self):
        """Return (username, device_id) for the active target device."""
        if not self.active_device:
            return None, None
        active = self.active_device.strip().lower()
        dev_entry = None
        for dev in self.registered_devices.values():
            if (dev.get("username") or "").strip().lower() == active or (dev.get("device_id") or "").strip().lower() == active:
                dev_entry = dev
                break
        dev_id = (dev_entry or {}).get("device_id") or active
        user_name = (dev_entry or {}).get("username") or active
        return user_name, dev_id

    def _switch_mqtt_subscriptions(self, old_device: Optional[str], uname: str):
        if not (self.client and self.client.is_connected()):
            return
        try:
            if old_device:
                self.client.unsubscribe(f"aiml/{old_device}/ack")
                self.client.unsubscribe(f"aiml/{old_device}/media")
                self.client.unsubscribe(f"aiml/{old_device}/status")

            self.client.subscribe(f"aiml/{uname}/ack", qos=MQTT_QOS)
            self.client.subscribe(f"aiml/{uname}/media", qos=MQTT_QOS)
            self.client.subscribe(f"aiml/{uname}/status", qos=MQTT_QOS)
            self.client.publish(f"aiml/{uname}/status", payload=ONLINE_PAYLOAD, qos=MQTT_QOS, retain=True)
            self.logger.info(f"[MQTT] Switched subscription from '{old_device}' to '{uname}' and published ONLINE status")
        except Exception as e:
            self.logger.error(f"[MQTT] Error switching device subscription: {e}")

    def set_active_device(self, username: str):
        username = (username or "").strip().lower()
        if not username:
            return False

        # Ensure device is in registry if selecting manually
        found = any(dev.get("username") == username or dev.get("device_id") == username for dev in self.registered_devices.values())
        if not found:
            self.registered_devices[username] = {
                "device_id": username,
                "username": username,
                "device_type": "android"
            }
            db_service.upsert_device(username, username, "android")

        old_device = self.active_device
        self.active_device = username
        uname, dev_id = self._get_active_identifiers()

        self._switch_mqtt_subscriptions(old_device, uname)

        self.logger.info(f"[MQTT] Active target mobile device set to: '{username}' (UUID: {dev_id})")
        self.socketio.emit('active_mobile_device_changed', {"active_device": username})
        is_on = self.is_device_online(username)
        self.socketio.emit('device_status_changed', {
            "device": username,
            "status": "online" if is_on else "offline",
            "online": is_on
        })
        return True

    def is_device_online(self, device_id_or_username: str) -> bool:
        """Check whether a device is currently marked online."""
        if not device_id_or_username:
            return False
        key = str(device_id_or_username).strip().lower()
        if key in self.device_online_statuses:
            return bool(self.device_online_statuses[key])
        for dev in self.registered_devices.values():
            if (dev.get("username") or "").strip().lower() == key or (dev.get("device_id") or "").strip().lower() == key:
                return bool(dev.get("online", False))
        return False

    def _set_device_online(self, identifier: str, is_online: bool):
        """Update online status for device across identifiers and registry entries."""
        if not identifier:
            return
        key = str(identifier).strip().lower()
        self.device_online_statuses[key] = is_online
        for dev in self.registered_devices.values():
            u = (dev.get("username") or "").strip().lower()
            d = (dev.get("device_id") or "").strip().lower()
            if u == key or d == key:
                dev["online"] = is_online
                dev["status"] = "ONLINE" if is_online else "OFFLINE"
                if u:
                    self.device_online_statuses[u] = is_online
                if d:
                    self.device_online_statuses[d] = is_online
                break

    def get_active_device_type(self):
        for dev in self.registered_devices.values():
            if dev.get("username") == self.active_device:
                return dev.get("device_type", "android")
        return "android"

    def _resolve_command_topic(self, active_username, target="mobile"):
        """Return the MQTT command topic for the active device.
        Unified for both Android & iOS: aiml/<device_username>/commands
        """
        if target == "ios":
            return f"aiml/{active_username}/commands/ios"
        return f"aiml/{active_username}/commands"

    def get_registered_devices(self):
        active_on = self.is_device_online(self.active_device)
        dev_list = []
        for dev in self.registered_devices.values():
            d_copy = dict(dev)
            d_copy["online"] = self.is_device_online(dev.get("username") or dev.get("device_id"))
            d_copy["status"] = "ONLINE" if d_copy["online"] else "OFFLINE"
            dev_list.append(d_copy)
        return {
            "active_device": self.active_device,
            "active_device_online": active_on,
            "devices": dev_list
        }

    def _wipe_cleared_mqtt_retained(self, old_devices, old_device):
        if not (self.client and self.client.is_connected()):
            return
        for d in old_devices:
            u = d.get("username")
            if u:
                try:
                    self.client.publish(f"aiml/{u}/status", payload=b"", qos=1, retain=True)
                    self.client.publish(f"aiml/{u}/commands", payload=b"", qos=1, retain=True)
                    self.client.publish(f"aiml/{u}/commands/ios", payload=b"", qos=1, retain=True)
                    self.client.publish(f"aiml/{u}/ack", payload=b"", qos=1, retain=True)
                except Exception:
                    pass
        try:
            self.client.publish(TOPIC_DISCOVERY, payload=b"", qos=1, retain=True)
        except Exception:
            pass
        if old_device:
            try:
                self.client.unsubscribe(f"aiml/{old_device}/ack")
                self.client.unsubscribe(f"aiml/{old_device}/media")
                self.client.unsubscribe(f"aiml/{old_device}/status")
            except Exception as e:
                self.logger.error(f"[MQTT] Error unsubscribing on clear: {e}")

    def clear_registered_devices(self):
        """Clears all dynamically registered devices from registry and wipes retained broker messages."""
        old_devices = list(self.registered_devices.values())
        old_device = self.active_device
        self.registered_devices.clear()
        self.active_device = None
        
        # Clear all devices from PostgreSQL
        db_service.delete_all_devices()
        
        self._wipe_cleared_mqtt_retained(old_devices, old_device)
        self.logger.info("[MQTT] Cleared all registered devices from registry and PostgreSQL database.")
        res = {
            "active_device": "",
            "devices": []
        }
        self.socketio.emit('mobile_devices_cleared', res)
        self.socketio.emit('active_mobile_device_changed', {"active_device": ""})
        return res

    def _wipe_device_retained_and_unsubscribe(self, target_uname: Optional[str]):
        if not (self.client and self.client.is_connected() and target_uname):
            return
        try:
            self.client.publish(f"aiml/{target_uname}/status", payload=b"", qos=1, retain=True)
            self.client.publish(f"aiml/{target_uname}/commands", payload=b"", qos=1, retain=True)
            self.client.publish(f"aiml/{target_uname}/commands/ios", payload=b"", qos=1, retain=True)
            self.client.publish(f"aiml/{target_uname}/ack", payload=b"", qos=1, retain=True)
        except Exception:
            pass

    def _handle_active_device_deleted(self, clean_id: str, target_uname: Optional[str]):
        if not self.active_device or self.active_device.strip().lower() not in (clean_id, str(target_uname or "").strip().lower()):
            return
        if self.client and self.client.is_connected() and target_uname:
            try:
                self.client.unsubscribe(f"aiml/{target_uname}/ack")
                self.client.unsubscribe(f"aiml/{target_uname}/media")
                self.client.unsubscribe(f"aiml/{target_uname}/status")
            except Exception as e:
                self.logger.error(f"[MQTT] Error unsubscribing on device delete: {e}")

        if self.registered_devices:
            next_dev = next(iter(self.registered_devices.values()))
            new_active = next_dev.get("username") or next_dev.get("device_id")
            self.set_active_device(new_active)
        else:
            self.active_device = None
            self.socketio.emit('active_mobile_device_changed', {"active_device": ""})

    def delete_registered_device(self, identifier: str):
        """Deletes a specific device by username or device_id from memory and database."""
        if not identifier:
            return self.get_registered_devices()

        clean_id = str(identifier).strip().lower()
        matched_keys = []
        target_uname = None
        target_dev_id = None

        for k, v in self.registered_devices.items():
            v_uname = str(v.get("username") or "").strip().lower()
            v_dev_id = str(v.get("device_id") or k).strip().lower()
            if clean_id in (v_uname, v_dev_id):
                matched_keys.append(k)
                target_uname = v.get("username")
                target_dev_id = v.get("device_id")

        if not matched_keys and identifier in self.registered_devices:
            matched_keys.append(identifier)
            target_uname = self.registered_devices[identifier].get("username")
            target_dev_id = self.registered_devices[identifier].get("device_id")

        for k in matched_keys:
            self.registered_devices.pop(k, None)

        if target_uname:
            self._set_device_online(target_uname, False)
        if target_dev_id:
            self._set_device_online(target_dev_id, False)

        # Delete from PostgreSQL
        db_service.delete_device(device_id=target_dev_id, username=target_uname or clean_id)

        # Wipe retained broker topics for this device
        self._wipe_device_retained_and_unsubscribe(target_uname)

        # If deleted device was currently active, choose another device or clear active
        self._handle_active_device_deleted(clean_id, target_uname)

        self.logger.info(f"[MQTT] Deleted device '{clean_id}' from registry and PostgreSQL database. Remaining: {len(self.registered_devices)}")
        res = self.get_registered_devices()
        self.socketio.emit('mobile_devices_updated', res)
        return res
    
    def _send_registration_response(self, device_id: Optional[str], disc_username: str, target_key: str, is_success: bool, err_msg: str = ""):
        if not (self.client and self.client.is_connected()):
            return
        if is_success:
            resp = json.dumps({
                "status": "SUCCESS",
                "username": disc_username,
                "device_id": target_key,
                "message": f"Device registered successfully as '{disc_username}'."
            })
            if device_id:
                self.client.publish(f"aiml/{device_id}/register_response", payload=resp, qos=1)
            self.client.publish(TOPIC_REGISTER_RESPONSE, payload=resp, qos=1)
            self.client.publish(topic=f"aiml/{disc_username}/status", payload=ONLINE_PAYLOAD, qos=MQTT_QOS, retain=False)
        else:
            err_resp = json.dumps({
                "status": "ERROR",
                "error": "USERNAME_TAKEN",
                "username": disc_username,
                "device_id": device_id,
                "message": err_msg
            })
            if device_id:
                self.client.publish(f"aiml/{device_id}/register_response", payload=err_resp, qos=1)
            self.client.publish(TOPIC_REGISTER_RESPONSE, payload=err_resp, qos=1)

    def _find_registration_conflict(self, disc_username: str, device_id: Optional[str]):
        try:
            db_devices = db_service.get_all_devices()
            db_usernames = {
                str(d.get("username") or "").strip().lower(): str(d.get("device_id") or "").strip().lower()
                for d in db_devices if d.get("username")
            }
        except Exception as e:
            self.logger.warning(f"[DB SYNC] Could not fetch devices from DB: {e}")
            db_usernames = None

        if db_usernames is not None and disc_username not in db_usernames:
            stale_keys = [k for k, v in self.registered_devices.items() if str(v.get("username") or "").strip().lower() == disc_username]
            for k in stale_keys:
                self.registered_devices.pop(k, None)
                self._set_device_online(k, False)
            return None

        for k, v in self.registered_devices.items():
            v_uid = str(v.get("device_id") or k).strip().lower()
            v_uname = str(v.get("username") or "").strip().lower()
            if v_uname == disc_username and device_id and v_uid != str(device_id).strip().lower():
                return v
        return None

    def _apply_registration_record(self, target_key: str, disc_username: str, disc_type: str, topic: str, device_id: Optional[str]):
        existing_key = None
        if device_id:
            for k, v in self.registered_devices.items():
                if str(v.get("device_id") or "").strip().lower() == str(device_id).strip().lower():
                    existing_key = k
                    break
        else:
            for k, v in self.registered_devices.items():
                if str(v.get("username") or "").strip().lower() == disc_username:
                    existing_key = k
                    break

        if existing_key:
            entry = self.registered_devices.pop(existing_key)
            old_username = entry.get("username")
            entry["device_id"] = target_key
            entry["username"] = disc_username
            entry["device_type"] = disc_type
            self.registered_devices[target_key] = entry
            db_service.upsert_device(target_key, disc_username, disc_type)

            if os.environ.get("SYNC_API_ENABLED", "false").lower() in ("true", "1", "yes"):
                try:
                    if sync_client:
                        sync_client.register_device_async(target_key, disc_username, disc_type)
                    sync_client.send_event_async(
                        kind="device_registration",
                        device_id=target_key,
                        status="SUCCESS",
                        topic=topic,
                        payload={"username": disc_username, "device_type": disc_type},
                    )
                except Exception:
                    pass

            if old_username != disc_username or entry.get("device_type") != disc_type:
                self.logger.info(f"[MQTT DEDUP] Updated device (UUID={target_key}): '{old_username}' -> '{disc_username}' [{disc_type}]")
                if self.active_device == old_username or not self.active_device:
                    self.set_active_device(disc_username)
                self.socketio.emit('mobile_device_registered', entry)
                self.socketio.emit('active_mobile_device_changed', {"active_device": self.active_device})
        else:
            rec = {"device_id": target_key, "username": disc_username, "device_type": disc_type}
            self.registered_devices[target_key] = rec
            db_service.upsert_device(target_key, disc_username, disc_type)

            if os.environ.get("SYNC_API_ENABLED", "false").lower() in ("true", "1", "yes"):
                try:
                    if sync_client:
                        sync_client.register_device_async(target_key, disc_username, disc_type)
                    sync_client.send_event_async(
                        kind="device_registration",
                        device_id=target_key,
                        status="SUCCESS",
                        topic=topic,
                        payload={"username": disc_username, "device_type": disc_type},
                    )
                except Exception:
                    pass

            if not self.active_device:
                self.set_active_device(disc_username)
            self._set_device_online(disc_username, True)
            if device_id:
                self._set_device_online(device_id, True)
            self.logger.info(f"[MQTT REGISTRATION] Discovered device (UUID={target_key}): '{disc_username}' [{disc_type}] via {topic}")
            self.socketio.emit('mobile_device_registered', rec)
            self.socketio.emit('device_status_changed', {"device": disc_username, "status": "online", "online": True})

        self._send_registration_response(device_id, disc_username, target_key, True)

    def _handle_registration_message(self, topic: str, payload: dict, parts: list):
        disc_username = payload.get("username") or (parts[3] if len(parts) >= 4 else None)
        if not disc_username:
            return
        disc_username = disc_username.strip().lower()
        if not disc_username:
            self.logger.warning("[MQTT] Ignoring registration with unconfigured/empty username")
            return

        disc_type = (payload.get("device_type") or "android").strip().lower()
        device_id = payload.get("device_id")
        target_key = device_id or disc_username

        # Fast path deduplication
        existing_entry = self.registered_devices.get(target_key)
        if not existing_entry and device_id:
            for k, v in self.registered_devices.items():
                if str(v.get("device_id") or k).strip().lower() == str(device_id).strip().lower():
                    existing_entry = v
                    target_key = k
                    break

        if existing_entry:
            ex_user = str(existing_entry.get("username") or "").strip().lower()
            ex_type = str(existing_entry.get("device_type") or "android").strip().lower()
            if ex_user == disc_username and ex_type == disc_type:
                if not self.active_device:
                    self.set_active_device(disc_username)
                self._set_device_online(disc_username, True)
                if device_id:
                    self._set_device_online(device_id, True)
                self._send_registration_response(device_id, disc_username, target_key, True)
                return

        conflict_owner = self._find_registration_conflict(disc_username, device_id)
        if conflict_owner:
            msg = f"Username '{disc_username}' is already registered by another device. Please choose a different username."
            self.logger.warning(f"[MQTT REGISTRATION CONFLICT] {msg}")
            self._send_registration_response(device_id, disc_username, target_key, False, msg)
            return

        self._apply_registration_record(target_key, disc_username, disc_type, topic, device_id)

    def on_message(self, client, userdata, msg):
        """Callback for handling incoming MQTT messages."""
        try:
            raw_payload = msg.payload.decode('utf-8')
            payload = json.loads(raw_payload) if raw_payload.strip().startswith(("{", "[")) else {}
            topic = msg.topic

            parts = topic.split("/")
            if topic == TOPIC_DISCOVERY or topic.startswith("aiml/devices/register"):
                self._handle_registration_message(topic, payload, parts)
                return

            active_username, active_uuid = self._get_active_identifiers()
            active_ids = {self.active_device, active_username, active_uuid}
            active_acks = {f"aiml/{i}/ack" for i in active_ids if i} | {TOPIC_ACK}
            active_medias = {f"aiml/{i}/media" for i in active_ids if i} | {TOPIC_MEDIA}
            active_statuses = {f"aiml/{i}/status" for i in active_ids if i} | {TOPIC_STATUS}

            if self._handle_ack_message(topic, payload, active_username, active_acks):
                return

            self._handle_status_message(topic, payload, raw_payload, parts, active_username, active_statuses)
            self._handle_media_message(topic, payload, parts, active_ids, active_medias)

            if payload.get("type") == "command" and DEBUG_MODE:
                self.logger.debug(f"[CMD] Broadcasted command payload: {payload.get('command')}")

        except Exception:
            self.logger.exception("[MQTT ERROR]")

    def _handle_ack_message(self, topic: str, payload: dict, active_username: str, active_acks: set) -> bool:
        is_ack_topic = (topic in active_acks) and payload.get("status") in ("EXECUTED", "SUCCESS")
        if is_ack_topic or payload.get("type") == "execution_feedback":
            self.logger.info(f"[APK] ACK RECEIVED from [{self.active_device}] :: {payload.get('command')}")
            payload["device"] = active_username
            self._set_device_online(active_username or self.active_device, True)
            self.socketio.emit('device_status_changed', {
                "device": active_username or self.active_device,
                "status": "online",
                "online": True
            })
            self.socketio.emit('execution_feedback', payload)
            if command_router:
                try:
                    cmd = payload.get("command")
                    target = "mobile" if self.get_active_device_type() == "android" else "ios"
                    command_router.update_command_by_correlation(
                        command=cmd,
                        transport="mqtt",
                        target_device=target,
                        status="executed",
                    )
                except Exception as e:
                    self.logger.debug(f"[COMMAND ROUTER] Could not update command status: {e}")
            return True
        return False

    def _handle_status_message(self, topic: str, payload: dict, raw_payload: str, parts: list, active_username: str, active_statuses: set):
        if topic in active_statuses or (len(parts) >= 3 and parts[0] == "bci" and parts[2] == "status"):
            status_raw = payload.get("status") if isinstance(payload, dict) else ""
            if not status_raw and isinstance(raw_payload, str):
                status_raw = raw_payload.strip()
            status_str = str(status_raw).upper()
            is_online = status_str in ("ONLINE", "CONNECTED", "READY", "1", "TRUE")
            sender_device = parts[1] if len(parts) >= 3 else (active_username or self.active_device)
            self._set_device_online(sender_device, is_online)
            self.socketio.emit('device_status_changed', {
                "device": sender_device,
                "status": "online" if is_online else "offline",
                "online": is_online
            })
        elif topic == TOPIC_HEARTBEAT or (len(parts) >= 3 and parts[0] == "bci" and parts[2] == "heartbeat"):
            hb_dev = payload.get("device_id") or (parts[1] if len(parts) >= 3 else active_username)
            if hb_dev:
                self._set_device_online(hb_dev, True)

    def _handle_media_message(self, topic: str, payload: dict, parts: list, active_ids: set, active_medias: set):
        msg_type = payload.get("type")
        is_media = (topic in active_medias or topic.endswith("/media") or msg_type == "media_update"
                    or any(k in payload for k in ["title", "song", "track", "song_name", "track_name"]))
        if not is_media:
            return

        if len(parts) >= 3 and parts[2] == "media" and parts[1] not in active_ids:
            return

        self._set_device_online(self.active_device, True)
        self.socketio.emit('device_status_changed', {
            "device": self.active_device,
            "status": "online",
            "online": True
        })

        song_title = payload.get("title") or payload.get("song") or payload.get("track") or payload.get("song_name") or "Playing Track"
        artist_name = payload.get("artist") or payload.get("singers") or payload.get("subtitle") or payload.get("artist_name") or "JioSaavn Artist"
        status_name = payload.get("status") or payload.get("state") or "Playing"
        media_key = (self.active_device, song_title, artist_name, status_name)
        if getattr(self, "_last_logged_media", None) != media_key:
            self._last_logged_media = media_key
            self.logger.info(f"[JIOSAAVN REALTIME] [{self.active_device}] {song_title} \u2014 {artist_name}")
        
        self.socketio.emit('media_update', {
            "title": song_title,
            "artist": artist_name,
            "album_art": payload.get("album_art") or payload.get("art") or payload.get("artwork") or payload.get("imageUrl") or "",
            "status": payload.get("status") or payload.get("state") or "Playing",
            "current_time": payload.get("current_time", "1:15"),
            "total_time": payload.get("total_time", "3:40"),
            "progress_percent": payload.get("progress_percent", 35),
            "volume": payload.get("volume", 70),
            "device": self.active_device
        })
    
    def connect(self):
        """Connect to MQTT broker and return the client instance."""
        if mqtt is None:
            self.logger.error("[MQTT] paho-mqtt package is not installed or available.")
            return None
        self.client = mqtt.Client(
            client_id=f"{CLIENT_ID_PREFIX}-{uuid.uuid4()}",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        self.client.on_message = self.on_message
        self.status_thread_running = True
        # Last will on the shared server-presence topic so iOS drops to
        # "Server offline" when this process dies unexpectedly.
        self.client.will_set(
            topic=SERVER_STATUS_TOPIC,
            payload=LAST_WILL_PAYLOAD,
            qos=MQTT_QOS,
            retain=True,
        )

        try:
            brokers_to_try = [MQTT_BROKER]
            if MQTT_FALLBACK_BROKER and MQTT_FALLBACK_BROKER not in brokers_to_try:
                brokers_to_try.append(MQTT_FALLBACK_BROKER)

            connected = False
            last_err = None
            for b in brokers_to_try:
                try:
                    logging.info(f"[MQTT] Connecting to {b}:{MQTT_PORT}...")
                    self.client.connect(b, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
                    self.active_broker = b
                    connected = True
                    logging.info(f"[MQTT] Connected successfully to {b}:{MQTT_PORT}")
                    break
                except Exception as err:
                    logging.warning(f"[MQTT] Failed to connect to {b}:{MQTT_PORT} ({err})")
                    last_err = err

            if not connected:
                raise ConnectionError(f"Could not connect to any MQTT broker in {brokers_to_try}: {last_err}")

            # Subscribe to discovery, wildcards, and active device topics
            topics_to_sub = [
                TOPIC_DISCOVERY,
                "aiml/devices/register",
                "aiml/devices/register/#",
                "aiml/+/status",
                "aiml/+/ack",
                "aiml/+/media",
                "aiml/+/heartbeat",
                TOPIC_ACK, TOPIC_STATUS, TOPIC_HEARTBEAT, TOPIC_MEDIA  # Backward compatibility
            ]
            if self.active_device:
                topics_to_sub.extend([
                    f"aiml/{self.active_device}/ack",
                    f"aiml/{self.active_device}/status",
                    f"aiml/{self.active_device}/media",
                    f"aiml/{self.active_device}/heartbeat",
                ])

            for topic in set(topics_to_sub):
                if topic:
                    self.client.subscribe(topic, qos=MQTT_QOS)
                    logging.info(f"[MQTT] Subscribed to {topic}")

            self.client.loop_start()

            if self.active_device:
                result = self.client.publish(
                    topic=f"aiml/{self.active_device}/status",
                    payload=ONLINE_PAYLOAD,
                    qos=MQTT_QOS,
                    retain=True,
                )
                if result.rc == 0:
                    logging.info(f"[MQTT] ONLINE status published for {self.active_device}")
                else:
                    logging.error(f"[MQTT] Failed to publish ONLINE status, code: {result.rc}")

            # Server presence for iOS (aiml/status). Retained so a phone that
            # connects after the dashboard sees ONLINE immediately.
            self.client.publish(
                topic=SERVER_STATUS_TOPIC,
                payload=ONLINE_PAYLOAD,
                qos=MQTT_QOS,
                retain=True,
            )
            logging.info(f"[MQTT] Server presence ONLINE on {SERVER_STATUS_TOPIC}")

            # Also publish to legacy TOPIC_STATUS if defined
            if TOPIC_STATUS:
                self.client.publish(topic=TOPIC_STATUS, payload=ONLINE_PAYLOAD, qos=MQTT_QOS, retain=True)

            logging.info(f"[MQTT] Active commands target: aiml/{self.active_device}/commands")
            
            # Notify frontend that MQTT is connected
            self.socketio.emit('mqtt_connection_status', {'status': 'CONNECTED', 'active_device': self.active_device})
            
            # Start lightweight periodic status publisher (every 30 seconds)
            self.start_status_publisher()
            
            return self.client
        except Exception:
            logging.exception("[MQTT ERROR] Failed:")
            return None
    
    def publish_command(self, raw_command: str, confidence: float, timestamp: float = None, target="mobile"):
        """Publish a command to the targeted MQTT device topic."""
        import time

        if target not in ("mobile", "ios"):
            raise ValueError(f"Unsupported MQTT command target: {target}")

        active_username, active_uuid = self._get_active_identifiers()
        command = (COMMAND_MAP.get(raw_command) or raw_command).strip()
        label = DISPLAY_LABELS.get(command, command)
        now_sec = int(time.time())
        cmd_id = f"cmd-{uuid.uuid4().hex[:8]}"

        payload = {
            "protocol": "masterhub",
            "version": "1.0",
            "message_type": "command",
            "origin": "masterhub_command",
            "command": command,
            "command_id": cmd_id,
            "target_device": active_uuid or active_username or "mobile",
            "confidence": round(float(confidence), 3) if confidence is not None else 0.95,
            "Is_Actionable": True,
            "timestamp": now_sec,
            "expires_at": now_sec + 60,
        }
        serialized_payload = json.dumps(payload)
        if self.client:
            target_topic = self._resolve_command_topic(active_username, target)
            self.client.publish(target_topic, serialized_payload, qos=MQTT_QOS)
            logging.info(f"[MQTT] publish command :: {command} -> topic='{target_topic}'")

        log_mqtt_payload(self._resolve_command_topic(active_username, target), payload, target)

        # Mirror search event to Sync API if enabled (using search-specific payload)
        if os.environ.get("SYNC_API_ENABLED", "false").lower() in ("true", "1", "yes"):
            try:
                if sync_client:
                    sync_client.send_inference_event_async(
                    device_id=active_uuid or active_username or "unknown_device",
                    raw_command="SEARCH",
                    canonical_command="SEARCH",
                    confidence=1.0,
                    command_id=cmd_id,
                    topic=self._resolve_command_topic(active_username, target),
                )
            except Exception as e:
                self.logger.debug(f"[SYNC API] Search event skipped: {e}")

        self.socketio.emit('command_dispatched', {
            "command": command,
            "display_label": label,
            "confidence": confidence,
            "timestamp": timestamp or time.time(),
            "target_device": active_uuid,
        })
    
    def publish_search_query(self, query: str, target="mobile"):
        """Forward to the single canonical JioSaavn search command dispatcher."""
        return self.publish_search_command(query, target=target)
    
    def _resolve_jiosaavn_url(self, query: str):
        import urllib.request
        import urllib.parse
        song_url = None
        song_title = query
        try:
            encoded = urllib.parse.quote(query.strip())
            api_url = f"https://www.jiosaavn.com/api.php?__call=autocomplete.get&_format=json&_marker=0&cc=in&includeMetaTags=1&query={encoded}"
            req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                for key in ('songs', 'topquery'):
                    if key in data and data[key].get('data') and len(data[key]['data']) > 0:
                        top = data[key]['data'][0]
                        song_url = top.get('url')
                        song_title = top.get('title', query)
                        break
        except Exception as e:
            self.logger.warning(f"[JIOSAAVN AUTOCOMPLETE] Failed to resolve URL for '{query}': {e}")
        return song_url, song_title

    def publish_search_command(self, query: str, target="mobile"):
        """Publish a SEARCH command with resolved song URL to targeted MQTT broker for JioSaavn."""
        import time

        if target not in ("mobile", "ios"):
            raise ValueError(f"Unsupported MQTT search target: {target}")
        active_username, active_uuid = self._get_active_identifiers()
        
        song_url, song_title = self._resolve_jiosaavn_url(query)
        now_sec = int(time.time())
        cmd_id = f"cmd-{uuid.uuid4().hex[:8]}"

        payload = {
            "protocol": "masterhub",
            "version": "1.0",
            "message_type": "command",
            "origin": "masterhub_command",
            "command": "SEARCH",
            "command_id": cmd_id,
            "target_device": active_uuid or active_username or "mobile",
            "confidence": 1.0,
            "Is_Actionable": True,
            "timestamp": now_sec,
            "expires_at": now_sec + 60,
            "query": query,
            "title": song_title,
            "song_url": song_url or "",
        }
        serialized_payload = json.dumps(payload)
        if self.client:
            target_topic = self._resolve_command_topic(active_username, target)
            self.client.publish(target_topic, serialized_payload, qos=MQTT_QOS)
            logging.info(f"[MQTT SEARCH] SEARCH query='{query}' url='{song_url}' -> topic='{target_topic}'")

        log_mqtt_payload(self._resolve_command_topic(active_username, target), payload, target)

        # Mirror search event to Sync API if enabled (using search-specific payload)
        if os.environ.get("SYNC_API_ENABLED", "false").lower() in ("true", "1", "yes"):
            try:
                if sync_client:
                    sync_client.send_inference_event_async(
                    device_id=active_uuid or active_username or "unknown_device",
                    raw_command="SEARCH",
                    canonical_command="SEARCH",
                    confidence=1.0,
                    command_id=cmd_id,
                    topic=self._resolve_command_topic(active_username, target),
                )
            except Exception as e:
                self.logger.debug(f"[SYNC API] Search event skipped: {e}")

        self.socketio.emit('command_dispatched', {
            "command": "SEARCH",
            "display_label": f"Play: {song_title}",
            "query": query,
            "song_url": song_url,
            "confidence": 1.0,
            "timestamp": time.time(),
            "target_device": active_uuid,
        })
        return {
            "status": "success",
            "query": query,
            "title": song_title,
            "song_url": song_url or "",
            "target": active_username,
        }
    
    def activate_manual_command_mode(self, duration_seconds=10):
        """
        Activate manual command mode to allow media updates for a specified duration.
        
        Args:
            duration_seconds: How long to allow media updates after manual command (default 10 seconds)
        """
        self.manual_command_active = True
        self.manual_command_timeout = time.time() + duration_seconds
        self.logger.info(f"[MQTT] Manual command mode activated for {duration_seconds} seconds")
        
        # Ensure media topic is subscribed for manual commands
        if not self.media_subscribed and self.client:
            try:
                self.client.subscribe(TOPIC_MEDIA, qos=MQTT_QOS)
                self.media_subscribed = True
                self.logger.info("[MQTT] Subscribed to media topic for manual command")
            except Exception as e:
                self.logger.error(f"[MQTT ERROR] Failed to subscribe to media topic: {e}")
        
        # Schedule automatic unsubscription after timeout
        def unsubscribe_after_timeout():
            time.sleep(duration_seconds)
            if self.manual_command_active and time.time() >= self.manual_command_timeout:
                self.manual_command_active = False
                self.manual_command_timeout = None
                # Only unsubscribe if pipeline is still paused
                if self.pipeline_scheduler and self.pipeline_scheduler.is_paused():
                    self.toggle_media_subscription(False)
        
        thread = threading.Thread(target=unsubscribe_after_timeout, daemon=True)
        thread.start()
    
    def toggle_media_subscription(self, should_subscribe):
        """
        Toggle media topic subscription based on pipeline state.
        
        Args:
            should_subscribe: True to subscribe, False to unsubscribe
        """
        if not self.client:
            return
            
        try:
            if should_subscribe and not self.media_subscribed:
                self.client.subscribe(TOPIC_MEDIA, qos=MQTT_QOS)
                self.media_subscribed = True
                self.logger.info("[MQTT] Subscribed to media topic")
            elif not should_subscribe and self.media_subscribed:
                self.client.unsubscribe(TOPIC_MEDIA)
                self.media_subscribed = False
                self.logger.info("[MQTT] Unsubscribed from media topic")
        except Exception as e:
            self.logger.error(f"[MQTT ERROR] Failed to toggle media subscription: {e}")
    
    def start_status_publisher(self):
        """
        Start a lightweight background thread to periodically republish the ONLINE status.
        This ensures that late-joining Android clients receive the web plugin status
        even if the MQTT broker's retained message mechanism isn't working properly.
        """
        def publish_status_periodically():
            while self.status_thread_running:
                try:
                    if self.client and self.client.is_connected():
                        # aiml/status must stay fresh: iOS treats the server as
                        # offline if this heartbeat is older than ~45s.
                        targets = {TOPIC_STATUS, SERVER_STATUS_TOPIC}
                        if self.active_device:
                            targets.add(f"aiml/{self.active_device}/status")
                        for t in targets:
                            self.client.publish(
                                topic=t,
                                payload=ONLINE_PAYLOAD,
                                qos=MQTT_QOS,
                                retain=False,
                            )
                except Exception:
                    logging.exception("[MQTT ERROR] Failed to republish status:")
                
                # Republish every 30 seconds
                time.sleep(30)
        
        status_thread = threading.Thread(target=publish_status_periodically, daemon=True)
        status_thread.start()
        logging.info("[MQTT] Started lightweight status publisher (30s interval)")
    
    def stop_status_publisher(self):
        """Stop the periodic status publisher."""
        self.status_thread_running = False




