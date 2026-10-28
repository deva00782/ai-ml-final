"""
SynaptiMesh Sync API Client.
============================
Provides an HTTP client for the SynaptiMesh Sync API microservice running on AWS EC2
accessed via local SSH tunnel (default: http://127.0.0.1:8000).

Features:
- Device Registration (POST /devices) with Policy A (409 Conflict rejection)
- Device Lookups (GET /devices/{id}, GET /devices/by-username/{username})
- BCI Telemetry & Inference Logging (POST /events)
- Bounded retries with exponential backoff reusing canonical event_id
- Resilient background worker pool with graceful queue draining on shutdown (atexit)
- Strict request timeouts to prevent blocking real-time BCI loops.
"""
from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


def _parse_env_file(path: Path):
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'").strip('"')
                if k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


def _load_env_fallback():
    _curr = Path(__file__).resolve().parent
    for _ in range(5):
        _env = _curr / ".env"
        if _env.is_file():
            _parse_env_file(_env)
            break
        _curr = _curr.parent

_load_env_fallback()

try:
    from dotenv import load_dotenv
    _curr = Path(__file__).resolve().parent
    for _ in range(5):
        _env_path = _curr / ".env"
        if _env_path.is_file():
            load_dotenv(_env_path, override=True)
            break
        _curr = _curr.parent
except ImportError:
    pass

import requests
import subprocess

logger = logging.getLogger("transmitter.sync_api")

# Configuration loaded strictly from .env / environment
SYNC_API_BASE_URL = os.environ.get("SYNC_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
SYNC_API_KEY = os.environ.get("SYNC_API_KEY", "").strip()
SYNC_INSTANCE_ID = os.environ.get("SYNC_INSTANCE_ID", "AIML_TRANSMITTER_01").strip()
SYNC_API_TIMEOUT = float(os.environ.get("SYNC_API_TIMEOUT", 5.0))
SYNC_MAX_RETRIES = int(os.environ.get("SYNC_MAX_RETRIES", 2))



# ==============================================================================
# Automatic Native Tunnel Manager (Zero PowerShell Needed)
# ==============================================================================
HEADER_APPLICATION_JSON = "application/json"
_auto_tunnel_proc: Optional[subprocess.Popen] = None
_auto_tunnel_lock = threading.Lock()


def _probe_sync_api_health() -> bool:
    try:
        r = requests.get(f"{SYNC_API_BASE_URL}/health", timeout=1.0)
        return r.status_code == 200
    except Exception:
        return False


def _reset_tunnel_process():
    global _auto_tunnel_proc
    if _auto_tunnel_proc is None:
        return
    if _auto_tunnel_proc.poll() is not None:
        _auto_tunnel_proc = None
    else:
        try:
            _auto_tunnel_proc.terminate()
            _auto_tunnel_proc.wait(timeout=1.0)
        except Exception:
            pass
        _auto_tunnel_proc = None


def _wait_for_tunnel_ready(proc: subprocess.Popen) -> bool:
    for _ in range(10):
        time.sleep(0.5)
        if proc.poll() is not None:
            err_msg = proc.stderr.read().strip() if proc.stderr else "Unknown error"
            logger.warning(f"[NATIVE-TUNNEL] SSH tunnel exited immediately (code {proc.returncode}): {err_msg}")
            return False
        if _probe_sync_api_health():
            logger.info("[NATIVE-TUNNEL] Database bridge connected successfully from Python.")
            return True
    return False


def _extract_local_sync_port() -> str:
    if ":" in SYNC_API_BASE_URL:
        try:
            return SYNC_API_BASE_URL.split(":")[-1].split("/")[0]
        except Exception:
            pass
    return "8001"


def _spawn_native_ssh_tunnel(key_path: Path, local_port: str):
    cmd = [
        "ssh",
        "-i", str(key_path),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=NUL",
        "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes",
        "-N",
        "-L", f"{local_port}:127.0.0.1:8000",
        "ec2-user@51.21.170.178"
    ]
    creation_flags = 0x08000000 if os.name == "nt" else 0
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=creation_flags,
        text=True
    )


def ensure_sync_tunnel() -> bool:
    """Ensure the connection to the Sync API is alive.
    If not responding, Python automatically manages the secure connection
    in the background natively without requiring external PowerShell scripts.
    """
    global _auto_tunnel_proc
    if _probe_sync_api_health():
        return True

    # Only attempt auto-bridge if configured for local loopback (dev mode)
    if "127.0.0.1" not in SYNC_API_BASE_URL and "localhost" not in SYNC_API_BASE_URL:
        return False

    with _auto_tunnel_lock:
        if _probe_sync_api_health():
            return True

        _reset_tunnel_process()

        key_path = Path.home() / ".ssh" / "synapti-test"
        if not key_path.exists():
            logger.warning(f"[NATIVE-TUNNEL] SSH key not found at {key_path}")
            return False

        local_port = _extract_local_sync_port()

        try:
            logger.info("[NATIVE-TUNNEL] Automatically opening database bridge from Python...")
            _auto_tunnel_proc = _spawn_native_ssh_tunnel(key_path, local_port)

            if _wait_for_tunnel_ready(_auto_tunnel_proc):
                return True

            if _auto_tunnel_proc and _auto_tunnel_proc.poll() is not None:
                err_msg = _auto_tunnel_proc.stderr.read().strip() if _auto_tunnel_proc.stderr else ""
                logger.warning(f"[NATIVE-TUNNEL] SSH tunnel failed: {err_msg}")
                _auto_tunnel_proc = None
            else:
                logger.warning("[NATIVE-TUNNEL] Bridge started but /health not responding yet.")
            return False
        except Exception:
            logger.exception("[NATIVE-TUNNEL] Failed to start bridge from Python:")
            return False

def _cleanup_auto_tunnel():
    global _auto_tunnel_proc
    if _auto_tunnel_proc:
        try:
            _auto_tunnel_proc.terminate()
            _auto_tunnel_proc.wait(timeout=2.0)
        except Exception:
            try:
                _auto_tunnel_proc.kill()
            except Exception:
                pass
        _auto_tunnel_proc = None

atexit.register(_cleanup_auto_tunnel)


class SyncAPIClient:
    """Singleton HTTP client session for efficient connection pooling and background draining."""

    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self.session = requests.Session()
        ensure_sync_tunnel()
        self._work_queue: queue.Queue = queue.Queue(maxsize=2000)
        self._running = True
        self._worker_thread = threading.Thread(target=self._worker_loop, name="SyncAPIWorker", daemon=True)
        self._worker_thread.start()
        self.dropped_events_count = 0
        atexit.register(self.shutdown)

    @classmethod
    def get_instance(cls) -> SyncAPIClient:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _get_headers(self) -> Dict[str, str]:
        key = os.environ.get("SYNC_API_KEY", SYNC_API_KEY).strip()
        headers = {
            "Content-Type": HEADER_APPLICATION_JSON,
            "Accept": HEADER_APPLICATION_JSON,
        }
        if key:
            headers["X-API-Key"] = key
        return headers

    def _drain_queue(self):
        while not self._work_queue.empty():
            try:
                rem = self._work_queue.get_nowait()
                if rem is not None:
                    func, args, kwargs = rem
                    try:
                        func(*args, **kwargs)
                    except Exception as e:
                        logger.warning(f"[SYNC API WORKER] Error draining task {func.__name__}: {e}")
                    finally:
                        self._work_queue.task_done()
            except Exception:
                break

    def _worker_loop(self):
        """Dedicated worker loop that drains tasks and terminates cleanly upon shutdown."""
        while True:
            try:
                task = self._work_queue.get(timeout=0.5)
            except queue.Empty:
                if not self._running:
                    break
                continue

            if task is None:
                self._drain_queue()
                self._work_queue.task_done()
                break

            func, args, kwargs = task
            try:
                func(*args, **kwargs)
            except Exception as e:
                logger.warning(f"[SYNC API WORKER] Error executing async task {func.__name__}: {e}")
            finally:
                self._work_queue.task_done()

    def enqueue(self, func, *args, **kwargs):
        """Enqueue task for async background processing without blocking real-time loops."""
        if not self._running:
            return
        try:
            self._work_queue.put_nowait((func, args, kwargs))
        except queue.Full:
            self.dropped_events_count += 1
            logger.error(
                f"[SYNC API] Queue full (dropped {self.dropped_events_count} event(s)). Check network or tunnel."
            )

    def shutdown(self, timeout: float = 5.0):
        """Drain remaining queued tasks on application exit with a bounded deadline."""
        if not self._running:
            return
        self._running = False
        remaining = self._work_queue.qsize()
        if remaining > 0:
            logger.info(f"[SYNC API] Draining {remaining} pending event(s) before exit (timeout={timeout}s)...")
        try:
            self._work_queue.put(None, timeout=1.0)
            self._worker_thread.join(timeout=timeout)
        except Exception as e:
            logger.warning(f"[SYNC API] Error during worker shutdown: {e}")


# ==============================================================================
# Synchronous Core Methods
# ==============================================================================

def health_check() -> Dict[str, Any]:
    """Verify endpoint connectivity (/health)."""
    client = SyncAPIClient.get_instance()
    url = f"{SYNC_API_BASE_URL}/health"
    try:
        resp = client.session.get(url, timeout=SYNC_API_TIMEOUT)
        return {
            "status": "ok" if resp.status_code == 200 else "error",
            "status_code": resp.status_code,
            "response": resp.json() if HEADER_APPLICATION_JSON in resp.headers.get("Content-Type", "") else resp.text,
        }
    except requests.exceptions.ConnectionError:
        return {
            "status": "error",
            "message": f"Connection refused on {url}. Ensure the SSH tunnel is active (Port 8000).",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _handle_register_response(resp, username: str, device_id: str) -> Optional[Dict[str, Any]]:
    if resp.status_code in (200, 201):
        logger.info(f"[SYNC API] Device '{username}' registered successfully.")
        return resp.json() if resp.content else {"status": "registered", "device_id": device_id}
    if resp.status_code == 409:
        logger.warning(f"[SYNC API 409] Conflict: Username '{username}' already in use by another device.")
        return {"status": "conflict", "error": "Username already taken", "status_code": 409}
    if resp.status_code == 401:
        logger.error("[SYNC API 401] Unauthorized: Invalid SYNC_API_KEY.")
        return {"status": "error", "error": "Unauthorized", "status_code": 401}
    return None


def register_device(device_id: str, username: str, device_type: str = "android") -> Dict[str, Any]:
    """Register or update a device registration in the Sync API (POST /devices).
    
    Adheres strictly to Policy A: Returns 409 Conflict if username is already registered to a different device.
    """
    client = SyncAPIClient.get_instance()
    url = f"{SYNC_API_BASE_URL}/devices"
    headers = client._get_headers()
    payload = {
        "device_id": str(device_id).strip(),
        "username": str(username).strip().lower(),
        "device_type": str(device_type).strip().lower(),
    }
    
    for attempt in range(1 + SYNC_MAX_RETRIES):
        try:
            resp = client.session.post(url, json=payload, headers=headers, timeout=SYNC_API_TIMEOUT)
            result = _handle_register_response(resp, username, device_id)
            if result is not None:
                return result
            if resp.status_code >= 500 and attempt < SYNC_MAX_RETRIES:
                time.sleep(0.5 * (2 ** attempt))
                continue
            logger.error(f"[SYNC API] Failed device registration ({resp.status_code}): {resp.text}")
            return {"status": "error", "status_code": resp.status_code, "detail": resp.text}
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < SYNC_MAX_RETRIES:
                ensure_sync_tunnel()
                time.sleep(0.5 * (2 ** attempt))
                continue
            logger.warning(f"[SYNC API] Cannot register device after retries (tunnel offline?): {e}")
            return {"status": "offline", "error": str(e)}
        except Exception:
            logger.exception("[SYNC API] Unexpected error in register_device:")
            return {"status": "error", "error": "Unexpected error"}


def get_device(device_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve device details by device_id (GET /devices/{device_id})."""
    client = SyncAPIClient.get_instance()
    url = f"{SYNC_API_BASE_URL}/devices/{device_id}"
    headers = client._get_headers()
    try:
        resp = client.session.get(url, headers=headers, timeout=SYNC_API_TIMEOUT)
        return resp.json() if resp.status_code == 200 else None
    except Exception:
        logger.exception(f"[SYNC API] Error fetching device {device_id}:")
        return None


def get_device_by_username(username: str) -> Optional[Dict[str, Any]]:
    """Retrieve device details by username (GET /devices/by-username/{username})."""
    client = SyncAPIClient.get_instance()
    url = f"{SYNC_API_BASE_URL}/devices/by-username/{username.strip().lower()}"
    headers = client._get_headers()
    try:
        resp = client.session.get(url, headers=headers, timeout=SYNC_API_TIMEOUT)
        return resp.json() if resp.status_code == 200 else None
    except Exception:
        logger.exception(f"[SYNC API] Error fetching username {username}:")
        return None


def _handle_send_event_response(resp, event_id: str) -> Optional[Dict[str, Any]]:
    if resp.status_code in (200, 201):
        logger.info(f"[SYNC API] Event stored successfully (ID={event_id[:8]}...)")
        return {"status": "stored", "event_id": event_id, "data": resp.json() if resp.content else {}}
    if resp.status_code == 409 or "already_exists" in resp.text:
        logger.warning(f"[SYNC API] Duplicate event_id {event_id} already exists.")
        return {"status": "already_exists", "event_id": event_id}
    if resp.status_code == 401:
        logger.error("[SYNC API 401] Unauthorized. Check SYNC_API_KEY.")
        return {"status": "error", "error": "Unauthorized", "status_code": 401}
    return None


def _attempt_send_event(client, url, event_payload, headers, event_id, attempt):
    try:
        resp = client.session.post(url, json=event_payload, headers=headers, timeout=SYNC_API_TIMEOUT)
        result = _handle_send_event_response(resp, event_id)
        if result is not None:
            return True, result
        if resp.status_code >= 500 and attempt < SYNC_MAX_RETRIES:
            time.sleep(0.5 * (2 ** attempt))
            return False, None
        logger.error(f"[SYNC API] Failed with status {resp.status_code}: {resp.text}")
        return True, {"status": "error", "status_code": resp.status_code, "detail": resp.text}
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        if attempt < SYNC_MAX_RETRIES:
            ensure_sync_tunnel()
            time.sleep(0.5 * (2 ** attempt))
            return False, None
        logger.warning(f"[SYNC API] Connection error sending event {event_id} after retries: {e}")
        return True, {"status": "offline", "message": str(e), "event_id": event_id}
    except Exception:
        logger.exception(f"[SYNC API] Unexpected error sending event {event_id}:")
        return True, {"status": "error", "message": "Unexpected error", "event_id": event_id}


def send_event(
    kind: str,
    device_id: str = "",
    status: str = "SUCCESS",
    payload: Optional[Dict[str, Any]] = None,
    command_id: Optional[str] = None,
    topic: str = "",
    event_id: Optional[str] = None,
    instance_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Send one event to the Sync API (POST /events) with deduplication and bounded retries."""
    client = SyncAPIClient.get_instance()
    url = f"{SYNC_API_BASE_URL}/events"
    headers = client._get_headers()

    # Preserve canonical event_id across all retry attempts for safe deduplication
    if not event_id:
        event_id = str(uuid.uuid4())

    event_payload = {
        "event_id": event_id,
        "instance_id": instance_id or SYNC_INSTANCE_ID,
        "kind": kind,
        "device_id": device_id or "unknown_device",
        "status": status,
        "command_id": command_id or f"cmd-{uuid.uuid4().hex[:8]}",
        "topic": topic or f"synaptimesh/{kind}",
        "payload": payload or {},
    }

    for attempt in range(1 + SYNC_MAX_RETRIES):
        done, res = _attempt_send_event(client, url, event_payload, headers, event_id, attempt)
        if done:
            return res
    return {"status": "offline", "message": "Max retries reached", "event_id": event_id}


def send_inference_event(
    device_id: str,
    raw_command: str,
    canonical_command: str,
    confidence: float,
    sample_index: int = 0,
    sampling_rate: int = 128,
    is_actionable: bool = True,
    model_name: str = "integrated_eeg_classifier",
    command_id: Optional[str] = None,
    topic: str = "",
) -> Dict[str, Any]:
    """Submit a real BCI model inference prediction event to the Sync API."""
    payload = {
        "domain": "AI_ML",
        "model_name": model_name,
        "raw_command": raw_command,
        "canonical_command": canonical_command,
        "confidence": round(float(confidence), 3),
        "sample_index": sample_index,
        "sampling_rate": sampling_rate,
        "is_actionable": is_actionable,
        "timestamp": int(time.time()),
    }
    return send_event(
        kind="inference_result",
        device_id=device_id or "aiml_transmitter",
        status="SUCCESS",
        payload=payload,
        command_id=command_id,
        topic=topic or f"aiml/{device_id or 'default'}/inference",
    )



def get_all_devices() -> List[Dict[str, Any]]:
    """Retrieve all registered devices from PostgreSQL via Sync API (GET /devices)."""
    client = SyncAPIClient.get_instance()
    url = f"{SYNC_API_BASE_URL}/devices"
    try:
        resp = client.session.get(url, headers=client._get_headers(), timeout=SYNC_API_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("devices", [])
        return []
    except Exception:
        logger.exception("[SYNC API] Error fetching devices:")
        return []


def delete_device(device_id: str) -> bool:
    """Delete a registered device via Sync API (DELETE /devices/{device_id})."""
    client = SyncAPIClient.get_instance()
    url = f"{SYNC_API_BASE_URL}/devices/{device_id}"
    try:
        resp = client.session.delete(url, headers=client._get_headers(), timeout=SYNC_API_TIMEOUT)
        return resp.status_code == 200
    except Exception:
        logger.exception(f"[SYNC API] Error deleting device {device_id}:")
        return False

def get_events(device_id: Optional[str] = None, kind: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
    """Retrieve historical runtime events (GET /api/events)."""
    client = SyncAPIClient.get_instance()
    url = f"{SYNC_API_BASE_URL}/api/events"
    headers = client._get_headers()
    params = {"limit": limit}
    if device_id:
        params["device_id"] = device_id
    if kind:
        params["kind"] = kind

    try:
        resp = client.session.get(url, params=params, headers=headers, timeout=SYNC_API_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, list) else data.get("events", [])
        return []
    except Exception:
        logger.exception("[SYNC API] Error fetching events:")
        return []


# ==============================================================================
# Asynchronous Non-Blocking Workers
# ==============================================================================

def send_event_async(
    kind: str,
    device_id: str = "",
    status: str = "SUCCESS",
    payload: Optional[Dict[str, Any]] = None,
    command_id: Optional[str] = None,
    topic: str = "",
    event_id: Optional[str] = None,
):
    """Enqueue an event to be sent asynchronously via the background thread pool."""
    SyncAPIClient.get_instance().enqueue(
        send_event,
        kind=kind,
        device_id=device_id,
        status=status,
        payload=payload,
        command_id=command_id,
        topic=topic,
        event_id=event_id,
    )


def register_device_async(device_id: str, username: str, device_type: str = "android"):
    """Enqueue device registration asynchronously."""
    SyncAPIClient.get_instance().enqueue(
        register_device,
        device_id=device_id,
        username=username,
        device_type=device_type,
    )


def send_inference_event_async(
    device_id: str,
    raw_command: str,
    canonical_command: str,
    confidence: float,
    sample_index: int = 0,
    sampling_rate: int = 128,
    is_actionable: bool = True,
    model_name: str = "integrated_eeg_classifier",
    command_id: Optional[str] = None,
    topic: str = "",
):
    """Enqueue an inference result asynchronously without blocking real-time EEG loops."""
    SyncAPIClient.get_instance().enqueue(
        send_inference_event,
        device_id=device_id,
        raw_command=raw_command,
        canonical_command=canonical_command,
        confidence=confidence,
        sample_index=sample_index,
        sampling_rate=sampling_rate,
        is_actionable=is_actionable,
        model_name=model_name,
        command_id=command_id,
        topic=topic,
    )

