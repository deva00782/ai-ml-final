"""Cortex Bridge for Emotiv EPOC X headset integration.

This module handles the WebSocket connection to Emotiv Cortex, performs the
required handshake sequence, extracts mental commands, and forwards them to
the FSM controller.

Cortex connection flow:
1. Connect to wss://localhost:6868 (with self-signed SSL cert handling)
2. requestAccess (user approves via EMOTIV Launcher)
3. authorize (get cortexToken)
4. queryHeadsets (find connected headset)
5. createSession (open session with headset)
6. subscribe("com") (subscribe to mental commands)
7. Receive mental command events and forward to FSM
"""

import json
import ssl
import threading
import time
import logging
import websocket
from typing import Callable, Optional, Dict, Any


class CortexBridge:
    """Bridge between Emotiv Cortex WebSocket and FSM controller.
    
    This class handles all Cortex-specific communication and extracts
    mental commands (push/pull/left/right) with their power/confidence values.
    Application logic (threshold checking, state handling, command mapping)
    is handled by the FSM controller.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        command_callback: Callable[[str, float], None],
        logger: Optional[logging.Logger] = None,
    ):
        """Initialize Cortex bridge.
        
        Args:
            client_id: Emotiv Cortex client ID from developer portal
            client_secret: Emotiv Cortex client secret from developer portal
            command_callback: Function to call with (command, power) when Cortex detects a mental command
            logger: Logger instance (uses default if None)
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.command_callback = command_callback
        self.logger = logger or logging.getLogger(__name__)
        
        # Cortex connection state
        self.ws: Optional[websocket.WebSocketApp] = None
        self.cortex_token: Optional[str] = None
        self.session_id: Optional[str] = None
        self.headset_id: Optional[str] = None
        self.is_connected = False
        self.is_running = False
        self.request_id = 0
        
        # Reconnection settings
        self.reconnect_delay = 5.0  # seconds
        self.max_reconnect_attempts = 10
        self.reconnect_attempts = 0

    def _next_request_id(self) -> int:
        """Generate next JSON-RPC request ID."""
        self.request_id += 1
        return self.request_id

    def _send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send a JSON-RPC request to Cortex.
        
        Args:
            method: Cortex API method name
            params: Parameters for the method
            
        Returns:
            Request dictionary with id, jsonrpc, method, and params
        """
        request = {
            "id": self._next_request_id(),
            "jsonrpc": "2.0",
            "method": method,
        }
        if params:
            request["params"] = params
        
        return request

    def _on_message(self, ws: websocket.WebSocketApp, message: str):
        """Handle incoming WebSocket messages from Cortex."""
        try:
            data = json.loads(message)
            
            # Handle mental command data stream (Cortex com)
            if "com" in data:
                self.logger.info(f"[BCI] Received command: {data['com']}")
                self._handle_mental_command(data["com"])
            # Handle live BCI JSON format from BCI team
            elif "command" in data:
                self._handle_live_bci_command(data)
            # Handle API responses
            elif "result" in data:
                self._handle_api_response(data)
            # Handle errors
            elif "error" in data:
                self.logger.error(f"[CORTEX] API error: {data['error']}")
                
        except json.JSONDecodeError as e:
            self.logger.error(f"[CORTEX] JSON decode error: {e}")
        except Exception:
            self.logger.exception("[CORTEX] Message handling error")

    def _handle_live_bci_command(self, data: Dict[str, Any]):
        """Handle live BCI JSON format: {"command":"RIGHT","confidence":0.91,"level":1,"timestamp":"..."}"""
        cmd = data.get("command")
        conf = data.get("confidence", data.get("power", 0.0))
        ts = data.get("timestamp")
        level = data.get("level")
        self.logger.info(f"[BCI] Received command: {cmd} confidence={conf} level={level} timestamp={ts}")
        ts_val = self._parse_timestamp(ts)
        self._handle_mental_command([cmd, conf], timestamp=ts_val)

    def _parse_timestamp(self, ts: Any) -> float:
        """Parse timestamp from various formats to epoch float."""
        if ts is None:
            return time.time()
        try:
            if isinstance(ts, (int, float)):
                return float(ts)
            if isinstance(ts, str):
                import datetime
                try:
                    return datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f").timestamp()
                except ValueError:
                    try:
                        return datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp()
                    except ValueError:
                        return time.time()
        except Exception:
            return time.time()
        return time.time()

    def _handle_mental_command(self, com_data: list, timestamp: float = None):
        """Extract and forward mental command from Cortex data.
        
        Args:
            com_data: Mental command data from Cortex, format: ["command", power]
                     e.g., ["push", 0.85]
            timestamp: optional timestamp from live BCI JSON
        """
        if not com_data or len(com_data) < 2:
            return
            
        command = com_data[0].lower().strip()
        power = float(com_data[1])
        # Use provided timestamp or current time
        ts = timestamp if timestamp is not None else time.time()
        
        # Map Cortex mental commands to FSM primitives
        # Cortex uses: push, pull, left, right (neutral)
        # NEUTRAL must be completely ignored per spec
        if command == "neutral":
            self.logger.info(f"[FILTER] NEUTRAL ignored (power={power:.3f}) - not forwarded to FSM")
            return
            
        # Validate command is a known FSM primitive
        from .fsm_controller import PRIMITIVE_COMMANDS
        if command not in PRIMITIVE_COMMANDS:
            self.logger.warning(f"[CORTEX] Unknown mental command: {command}")
            return
            
        self.logger.info(f"[CORTEX] Mental command received: {command} (power={power:.3f})")
        
        # Forward to FSM controller via callback (preserve timestamp)
        if self.command_callback:
            try:
                # Try 3-arg callback (command, power, timestamp) for live BCI, fallback to 2-arg
                try:
                    self.command_callback(command, power, ts)
                except TypeError:
                    self.command_callback(command, power)
            except Exception as e:
                self.logger.error(f"[CORTEX] Command callback error: {e}")

    def _handle_api_response(self, data: Dict[str, Any]):
        """Handle Cortex API responses for handshake sequence."""
        result = data.get("result", {})
        request_id = data.get("id")
        
        # Handle authorize response
        if isinstance(result, dict) and "cortexToken" in result:
            self._handle_authorize_response(result)
        
        # Handle queryHeadsets response
        elif isinstance(result, list) and request_id and result and isinstance(result[0], dict) and "id" in result[0]:
            self._handle_query_headsets_response(result)
        
        # Handle empty headset list
        elif isinstance(result, list) and request_id and len(result) == 0:
            self.logger.error("[CORTEX] No headsets found (empty list)")
        
        # Handle createSession response
        elif isinstance(result, dict) and "id" in result and "cortexToken" not in result:
            self._handle_create_session_response(result)
        
        # Handle subscribe response
        elif isinstance(result, dict) and ("success" in result or "failure" in result):
            self._handle_subscribe_response(result)
        elif isinstance(result, list) and request_id:
            self._handle_subscribe_response_legacy(result)

    def _handle_authorize_response(self, result: Dict[str, Any]):
        self.cortex_token = result["cortexToken"]
        self.logger.info("[CORTEX] Authorization successful, token received")
        if "warning" in result:
            self.logger.warning(f"[CORTEX] EULA warning: {result['warning']}")

    def _handle_query_headsets_response(self, result: list):
        if result:
            for headset in result:
                if headset.get("status") == "connected":
                    self.headset_id = headset.get("id")
                    self.logger.info(f"[CORTEX] Found connected headset: {self.headset_id}")
                    break
            if not self.headset_id:
                self.headset_id = result[0].get("id")
                self.logger.warning(f"[CORTEX] No connected headset, using: {self.headset_id}")
        else:
            self.logger.error("[CORTEX] No headsets found")

    def _handle_create_session_response(self, result: Dict[str, Any]):
        self.session_id = result["id"]
        self.logger.info(f"[CORTEX] Session created: {self.session_id}")

    def _handle_subscribe_response(self, result: Dict[str, Any]):
        self.logger.info(f"[CORTEX] Subscription successful: {result}")
        if "success" in result:
            self.logger.info("[CORTEX] Live stream started - ready to receive mental commands")

    def _handle_subscribe_response_legacy(self, result: list):
        self.logger.info(f"[CORTEX] Subscription successful for streams: {result}")
        self.logger.info("[CORTEX] Live stream started - ready to receive mental commands")

    def _on_error(self, ws: websocket.WebSocketApp, error: Exception):
        """Handle WebSocket errors."""
        self.logger.error(f"[CORTEX] WebSocket error: {error}")

    def _on_close(self, ws: websocket.WebSocketApp, close_status_code, close_msg):
        """Handle WebSocket connection close."""
        self.is_connected = False
        self.logger.warning(f"[CORTEX] Connection closed: {close_status_code} - {close_msg}")
        
        # Attempt reconnection if still running
        if self.is_running:
            self._schedule_reconnect()

    def _on_open(self, ws: websocket.WebSocketApp):
        """Handle WebSocket connection open."""
        self.is_connected = True
        self.reconnect_attempts = 0
        self.logger.info("[CORTEX] WebSocket connection established")
        
        # Keep the WebSocket callback thread free to receive handshake responses.
        handshake_thread = threading.Thread(target=self._perform_handshake, daemon=True)
        handshake_thread.start()

    def _wait_for(self, condition_fn, timeout=10.0, interval=0.1, desc="condition"):
        """Wait for condition_fn to become True with timeout."""
        start = time.time()
        while time.time() - start < timeout:
            if condition_fn():
                return True
            time.sleep(interval)
        self.logger.warning(f"[CORTEX] Timeout waiting for {desc} after {timeout}s")
        return False

    def _perform_handshake(self):
        """Perform Cortex API handshake sequence with proper response waiting."""
        try:
            # Step 1: requestAccess (user must approve via EMOTIV Launcher)
            self.logger.info("[CORTEX] Step 1: Requesting access...")
            request = self._send_request("requestAccess", {
                "clientId": self.client_id,
                "clientSecret": self.client_secret,
            })
            self.ws.send(json.dumps(request))
            time.sleep(1)  # Give Cortex time to process requestAccess
            
            # Step 2: authorize - must wait for cortexToken before proceeding
            self.logger.info("[CORTEX] Step 2: Authorizing...")
            request = self._send_request("authorize", {
                "clientId": self.client_id,
                "clientSecret": self.client_secret,
            })
            self.ws.send(json.dumps(request))
            # Wait for authorize response to populate cortex_token
            if not self._wait_for(lambda: self.cortex_token is not None, timeout=30.0, desc="cortexToken"):
                self.logger.error("[CORTEX] Failed to obtain cortexToken - authorize did not return token")
                return
            self.logger.info(f"[CORTEX] Token acquired: {self.cortex_token[:10]}...")

            # Step 3: queryHeadsets
            self.logger.info("[CORTEX] Step 3: Querying headsets...")
            request = self._send_request("queryHeadsets")
            self.ws.send(json.dumps(request))
            # Wait briefly for headset query response (or timeout)
            self._wait_for(lambda: self.headset_id is not None, timeout=5.0, desc="headsetId")
            # Even if no headset_id yet, proceed - headset may be detected later
            if self.headset_id:
                self.logger.info(f"[CORTEX] Using headset: {self.headset_id}")
            else:
                self.logger.warning("[CORTEX] No headsetId yet, will use fallback or let Cortex choose")
            
            # Step 4: createSession - requires valid cortexToken
            self.logger.info("[CORTEX] Step 4: Creating session...")
            if not self.cortex_token:
                self.logger.error("[CORTEX] Cannot create session without cortexToken")
                return
            if self.headset_id:
                request = self._send_request("createSession", {
                    "cortexToken": self.cortex_token,
                    "headset": self.headset_id,
                    "status": "active",
                })
            else:
                # Let Cortex use first available headset
                request = self._send_request("createSession", {
                    "cortexToken": self.cortex_token,
                    "status": "active",
                })
            self.ws.send(json.dumps(request))
            # Wait for session creation
            if not self._wait_for(lambda: self.session_id is not None, timeout=10.0, desc="sessionId"):
                self.logger.error("[CORTEX] Failed to create session - sessionId not received")
                return
            
            # Step 5: subscribe to mental commands
            self.logger.info("[CORTEX] Step 5: Subscribing to mental commands...")
            request = self._send_request("subscribe", {
                "cortexToken": self.cortex_token,
                "session": self.session_id,
                "streams": ["com"],
            })
            self.ws.send(json.dumps(request))
            # Subscribe response will be handled in _handle_api_response and will log Live stream started
            
            self.logger.info("[CORTEX] Handshake complete, ready to receive mental commands")
            
        except Exception as e:
            self.logger.error(f"[CORTEX] Handshake error: {e}")

    def _schedule_reconnect(self):
        """Schedule reconnection attempt."""
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            self.logger.error("[CORTEX] Max reconnection attempts reached, giving up")
            self.is_running = False
            return
        
        self.reconnect_attempts += 1
        delay = self.reconnect_delay * self.reconnect_attempts  # Exponential backoff
        
        self.logger.info(f"[CORTEX] Scheduling reconnection attempt {self.reconnect_attempts}/{self.max_reconnect_attempts} in {delay}s")
        
        def reconnect():
            time.sleep(delay)
            if self.is_running:
                self.connect()
        
        thread = threading.Thread(target=reconnect, daemon=True)
        thread.start()

    def connect(self) -> bool:
        """Connect to Cortex WebSocket server.
        
        Returns:
            True if connection initiated successfully, False otherwise
        """
        if self.is_connected:
            self.logger.warning("[CORTEX] Already connected")
            return True
        
        try:
            # Configure SSL to handle self-signed certificate
            sslopt = {"cert_reqs": ssl.CERT_NONE}
            
            self.ws = websocket.WebSocketApp(
                "wss://localhost:6868",
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
                on_open=self._on_open,
            )
            
            self.is_running = True
            self.logger.info("[CORTEX] Connecting to wss://localhost:6868...")
            
            # Run WebSocket in separate thread
            def run_ws():
                self.ws.run_forever(sslopt=sslopt)
            
            ws_thread = threading.Thread(target=run_ws, daemon=True)
            ws_thread.start()
            
            return True
            
        except Exception as e:
            self.logger.error(f"[CORTEX] Connection error: {e}")
            return False

    def disconnect(self):
        """Disconnect from Cortex WebSocket server."""
        self.is_running = False
        if self.ws:
            self.ws.close()
            self.logger.info("[CORTEX] Disconnected")

    def is_cortex_connected(self) -> bool:
        """Check if Cortex bridge is connected."""
        return self.is_connected


def create_cortex_bridge(
    client_id: str,
    client_secret: str,
    command_callback: Callable[[str, float], None],
    logger: Optional[logging.Logger] = None,
) -> CortexBridge:
    """Factory function to create a Cortex bridge instance.
    
    Args:
        client_id: Emotiv Cortex client ID
        client_secret: Emotiv Cortex client secret
        command_callback: Function to call with (command, power)
        logger: Logger instance
        
    Returns:
        Configured CortexBridge instance
    """
    return CortexBridge(
        client_id=client_id,
        client_secret=client_secret,
        command_callback=command_callback,
        logger=logger,
    )