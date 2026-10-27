"""Best-effort UART transport for canonical dashboard actions.

Serial I/O is isolated on a worker thread so dashboard request handlers and
the Emotiv/FSM pipeline never wait for a receiver response.
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

try:
    from . import boxed_logger
except ImportError:
    try:
        import boxed_logger
    except ImportError:
        boxed_logger = None

try:
    import serial.tools.list_ports
except ImportError:
    serial = None

try:
    from backend.payload_logger import log_uart_payload
except ImportError:
    def log_uart_payload(command: str, payload_type: str = "action"):
        pass

logger = logging.getLogger()

SILICON_LABS_VID = 0x10C4
COMMON_UART_VIDS = {
    0x10C4,  # Silicon Labs CP210x
    0x0403,  # FTDI FT232 / FT2232 / FT4232 / FT230X
    0x1A86,  # WCH CH340 / CH341 / CH343 / CH9102
    0x067B,  # Prolific PL2303
    0x7523,  # QinHeng CH340 / USB-Serial
    0x303A,  # Espressif ESP32 USB CDC
    0x2E8A,  # Raspberry Pi RP2040 / Pico
    0x2341,  # Arduino
    0x2A03,  # Arduino
}

KNOWN_CP210X_PIDS = {
    0xEA60,  # CP2102 / CP2103 / CP2104 / CP2109
    0xEA70,  # CP2105 Dual UART
    0xEA71,  # CP2108 Quad UART
    0xEA61,  # CP2101
    0xEA63,  # CP2102N
    0xEA80,  # CP2110
}

UART_KEYWORDS = (
    "CP210", "SILICON LABS", "FTDI", "FT232", "CH340", "CH341", "CH343",
    "PL2303", "PROLIFIC", "USB SERIAL", "USB-SERIAL", "UART", "CDC",
)

BOX_TITLE_UART_CONNECTION = "UART CONNECTION"
BOX_TITLE_UART_TRANSMIT = "UART TRANSMIT"


def _env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _decode_terminator(value):
    return value.encode("utf-8").decode("unicode_escape")


def _is_bluetooth_port(p) -> bool:
    """Check if port is a virtual Bluetooth serial link (should be ignored for UART)."""
    hwid_str = (p.hwid or "").upper()
    desc_str = (p.description or "").upper()
    return "BTHENUM" in hwid_str or "BLUETOOTH" in desc_str or "BTH" in hwid_str


def _is_uart_port(p) -> bool:
    """Check if port matches known USB-to-UART bridge signatures."""
    if _is_bluetooth_port(p):
        return False
    if p.vid in COMMON_UART_VIDS:
        return True
    hwid_str = (p.hwid or "").upper()
    desc_str = (p.description or "").upper()
    if any(sig in hwid_str or sig in desc_str for sig in UART_KEYWORDS):
        return True
    if p.vid is not None and "USB" in hwid_str:
        return True
    return False


def _select_best_port(candidates):
    if not candidates:
        return None
    # Prefer CP210x or known UART VIDs
    for c in candidates:
        pid = c.get("pid")
        if pid and pid != "Unknown":
            try:
                if int(pid, 16) in KNOWN_CP210X_PIDS:
                    return c["port"]
            except ValueError:
                pass
    return candidates[0]["port"]


def find_uart_port():
    """Auto-detect any connected USB-to-UART bridge adapter (CP210x, FTDI, CH340, PL2303, CDC, etc.).
    Returns port device string (e.g., 'COM4', 'COM7') or None if not found.
    """
    if serial is None:
        return None
    try:
        candidates = []
        for p in serial.tools.list_ports.comports():
            if _is_uart_port(p):
                vid_hex = f"0x{p.vid:04X}" if p.vid is not None else "Unknown"
                pid_hex = f"0x{p.pid:04X}" if p.pid is not None else "Unknown"
                candidates.append({
                    "port": p.device,
                    "description": p.description,
                    "vid": vid_hex,
                    "pid": pid_hex,
                    "hwid": p.hwid,
                    "serial_number": getattr(p, "serial_number", None) or "N/A",
                })
        if candidates:
            return _select_best_port(candidates)

        # Fallback: any non-Bluetooth port with USB in HWID/description
        for p in serial.tools.list_ports.comports():
            if not _is_bluetooth_port(p):
                desc_or_hwid = ((p.description or "") + " " + (p.hwid or "")).upper()
                if "USB" in desc_or_hwid:
                    return p.device
    except Exception as e:
        logger.warning(f"Error while scanning for UART adapter: {e}")
    return None


def find_silicon_labs_port():
    """Backward-compatible alias for find_uart_port()."""
    return find_uart_port()


def _default_port():
    """Utility alias for find_uart_port()."""
    return find_uart_port()


class UARTTransmitter:
    """Transmit canonical actions without making UART a dashboard dependency."""

    def __init__(
        self,
        enabled=True,
        port="AUTO",
        baudrate=9600,
        timeout=2.5,
        ack_enabled=False,
        terminator="\n",
    ):
        self.enabled = bool(enabled)
        self.port = port
        self.baudrate = int(baudrate)
        self.timeout = float(timeout)
        self.ack_enabled = bool(ack_enabled)
        self.terminator = terminator
        self._serial = None
        self._serial_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uart")
        self._resolved_port = None
        self._port_source = None

    def _resolve_port(self):
        """Resolve port dynamically using priority: explicit arg -> env var -> auto-detect -> None."""
        if self.port and str(self.port).strip().upper() not in ("", "AUTO", "NONE"):
            self._resolved_port = str(self.port).strip()
            self._port_source = "explicit argument"
            return

        # Check environment variables
        for env_var in ("UART_PORT", "RECEIVER_PORT", "SERIAL_PORT", "TRANSMITTER_PORT"):
            env_val = os.environ.get(env_var)
            if env_val and env_val.strip().upper() not in ("", "AUTO", "NONE"):
                self._resolved_port = env_val.strip()
                self._port_source = f"{env_var} env"
                return

        # Auto-detect any USB-to-UART bridge adapter
        detected = find_uart_port()
        if detected:
            self._resolved_port = detected
            self._port_source = f"Auto-detected USB-UART ({detected})"
            return

        self._resolved_port = None
        self._port_source = "Not detected"

    @classmethod
    def from_env(cls):
        return cls(
            enabled=_env_bool("UART_ENABLED", True),
            port=os.environ.get("UART_PORT", "AUTO"),
            baudrate=int(os.environ.get("UART_BAUDRATE", "9600")),
            timeout=float(os.environ.get("UART_TIMEOUT", "2.5")),
            ack_enabled=_env_bool("UART_ACK_ENABLED", True),
            terminator=_decode_terminator(os.environ.get("UART_TERMINATOR", "\\n")),
        )

    def _handle_connect_failure(self, exc):
        self._serial = None
        available_ports = []
        if serial is not None:
            try:
                available_ports = [f"{p.device} ({p.description})" for p in serial.tools.list_ports.comports() if not _is_bluetooth_port(p)]
            except Exception:
                pass

        if boxed_logger:
            boxed_logger.print_box(BOX_TITLE_UART_CONNECTION, [
                ("Port", self._resolved_port),
                ("Source", self._port_source),
                ("Time", boxed_logger.now_str()),
                ("Status", "FAILED"),
                ("Error", exc),
                ("Available Ports", "; ".join(available_ports) if available_ports else "None detected"),
            ])
        logger.warning("[UART ERROR] Unable to open %s: %s", self._resolved_port, exc)
        if available_ports:
            logger.info("[UART] Detected system ports: %s", "; ".join(available_ports))
        return False

    def connect(self):
        if not self.enabled:
            logger.info("[UART] Disabled")
            if boxed_logger:
                boxed_logger.print_box(BOX_TITLE_UART_CONNECTION, [
                    ("Port", self._resolved_port or self.port),
                    ("Baud", self.baudrate),
                    ("Time", boxed_logger.now_str()),
                    ("Status", "DISABLED"),
                ])
            return False

        # Dynamically re-resolve port if not set or was not detected
        if self._resolved_port is None or self.port in ("AUTO", None, ""):
            self._resolve_port()

        if not self._resolved_port:
            logger.debug("[UART] No UART port detected on system")
            return False

        with self._serial_lock:
            if self._serial is not None and getattr(self._serial, "is_open", False):
                return True
            try:
                import serial

                logger.info("[UART] Connecting to %s @ %s (source: %s)", self._resolved_port, self.baudrate, self._port_source)
                self._serial = serial.Serial(
                    port=self._resolved_port,
                    baudrate=self.baudrate,
                    timeout=self.timeout,
                    write_timeout=self.timeout,
                    rtscts=False,
                    dsrdtr=False,
                    xonxoff=False,
                )
                self._serial.dtr = False
                self._serial.rts = False
                self._serial.reset_input_buffer()
                self._serial.reset_output_buffer()
                if boxed_logger:
                    boxed_logger.print_box(BOX_TITLE_UART_CONNECTION, [
                        ("Port", self._resolved_port),
                        ("Baud", f"{self.baudrate}"),
                        ("Source", self._port_source),
                        ("Time", boxed_logger.now_str()),
                        ("Status", "CONNECTED"),
                    ])
                logger.info("[UART] Connected")
                return True
            except Exception as exc:
                return self._handle_connect_failure(exc)

    def disconnect(self):
        with self._serial_lock:
            serial_port = self._serial
            self._serial = None
            if serial_port is not None:
                try:
                    serial_port.close()
                except Exception as exc:
                    logger.exception("[UART ERROR] Serial close failed: %s", exc)

    def is_connected(self):
        with self._serial_lock:
            if self._serial is not None and getattr(self._serial, "is_open", False):
                return True
        if self.enabled:
            return self.connect()
        return False

    def _get_serial(self):
        if self.is_connected() or not self.connect():
            with self._serial_lock:
                return self._serial
        with self._serial_lock:
            return self._serial

    def send_command(self, action):
        """Synchronously send one canonical action; intended for the worker/test utility."""
        if not self.enabled:
            return None
        action = str(action).strip()
        if not action:
            logger.error("[UART ERROR] Empty action was not transmitted")
            return None

        # Normalize common action variants for wire compatibility with all receiver versions
        wire_map = {
            "Volume Up": "volume_up",
            "Volume Down": "volume_down",
            "volume up": "volume_up",
            "volume down": "volume_down",
            "Right_Close_App": "back_to_level_2",
            "Back to Level 2": "back_to_level_2",
            "back to level 2": "back_to_level_2",
            "PUSH+RIGHT": "back_to_level_2",
            "push+right": "back_to_level_2",
            "push + right": "back_to_level_2",
            "close_app": "back_to_level_2",
        }
        wire_action = wire_map.get(action, action)

        return self.send_raw(f"action cmd|{wire_action}")

    def _read_ack_responses(self, serial_port, command):
        ack_responses = []
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if serial_port.in_waiting > 0:
                response = serial_port.read_until()
                if response:
                    text = response.decode("utf-8", errors="replace").strip()
                    if text:
                        ack_responses.append(text)
                        logger.info("[UART RX] %s", text)
                        if boxed_logger:
                            boxed_logger.print_box("UART ACKNOWLEDGEMENT", [
                                ("Port", self._resolved_port),
                                ("Action", command),
                                ("Response", text[:80] + ("..." if len(text) > 80 else "")),
                                ("Time", boxed_logger.now_str()),
                                ("Status", "ACK RECEIVED"),
                            ])
                        try:
                            import json
                            parsed = json.loads(text)
                            if parsed.get("result") in ("SUCCESS", "FAILED", "PONG"):
                                break
                        except Exception:
                            pass
            time.sleep(0.01)
        return ack_responses

    def _log_ack_summary(self, ack_responses, command):
        if ack_responses:
            if boxed_logger:
                boxed_logger.print_box(BOX_TITLE_UART_TRANSMIT, [
                    ("Port", self._resolved_port),
                    ("Action", command),
                    ("Time", boxed_logger.now_str()),
                    ("Status", f"TRANSMITTED - {len(ack_responses)} ACK(s) RECEIVED"),
                ])
            return ack_responses

        if boxed_logger:
            boxed_logger.print_box(BOX_TITLE_UART_TRANSMIT, [
                ("Port", self._resolved_port),
                ("Action", command),
                ("Time", boxed_logger.now_str()),
                ("Status", "TRANSMITTED - NO ACK"),
            ])
        logger.warning(
            "[UART] Command WAS transmitted to %s @ %d baud, but receiver sent no ACK within %.1fs "
            "- check receiver BAUD matches, wiring TX->RX / RX->TX / GND->GND, "
            "and that the receiver sends back an ACK line",
            self._resolved_port, self.baudrate, self.timeout,
        )
        return []

    def send_raw(self, command):
        """Synchronously send exact wire text, without adding a command prefix.
        
        If ack_enabled, reads all available ACK responses (receiver sends RECEIVED then SUCCESS/FAILED).
        Returns list of ACK responses received.
        """
        if not self.enabled:
            return None
        command = str(command).strip()
        if not command:
            logger.error("[UART ERROR] Empty command was not transmitted")
            return None

        # Ensure port is resolved
        if self._resolved_port is None:
            self._resolve_port()

        payload = f"{command}{self.terminator}".encode("utf-8")
        display_payload = payload.decode("utf-8", errors="replace").encode("unicode_escape").decode("ascii")
        logger.info("[UART TX] %s", display_payload)

        logging.info(f"[TX PAYLOAD] UART -> {command.strip()}")

        log_uart_payload(command, "action" if "cmd|" in command else "search")

        serial_port = self._get_serial()
        if serial_port is None:
            if boxed_logger:
                boxed_logger.print_box(BOX_TITLE_UART_TRANSMIT, [
                    ("Port", self._resolved_port),
                    ("Action", command),
                    ("Time", boxed_logger.now_str()),
                    ("Status", "NOT SENT - PORT UNAVAILABLE"),
                ])
            logger.error("[UART] NOT SENT - %s unavailable", self._resolved_port)
            return None

        try:
            with self._serial_lock:
                serial_port.write(payload)
                serial_port.flush()
                if not self.ack_enabled:
                    if boxed_logger:
                        boxed_logger.print_box(BOX_TITLE_UART_TRANSMIT, [
                            ("Port", self._resolved_port),
                            ("Action", command),
                            ("Time", boxed_logger.now_str()),
                            ("Status", "TRANSMITTED (ACK disabled)"),
                        ])
                    return []

            ack_responses = self._read_ack_responses(serial_port, command)
            return self._log_ack_summary(ack_responses, command)
        except Exception as exc:
            if boxed_logger:
                boxed_logger.print_box(BOX_TITLE_UART_TRANSMIT, [
                    ("Port", self._resolved_port),
                    ("Action", command),
                    ("Time", boxed_logger.now_str()),
                    ("Status", "FAILED"),
                ])
            logger.exception("[UART ERROR] Serial transmission failed: %s", exc)
            self.disconnect()
            return None

    def send_action(self, action):
        """Queue an action and return immediately to the dashboard caller."""
        if not self.enabled:
            return None
        try:
            return self._executor.submit(self.send_command, action)
        except Exception as exc:
            logger.exception("[UART ERROR] Could not queue command: %s", exc)
            return None

    @staticmethod
    def sanitize_query(query, max_len=200):
        """Clean a song-name query for the UART wire (single line, no separators)."""
        q = str(query or "").strip().replace("\r", " ").replace("\n", " ")
        q = q.replace("|", "/")
        if len(q) > max_len:
            q = q[:max_len].rstrip()
        return q

    def send_search_command(self, query):
        """Synchronously send a SEARCH with song-name query; receiver parses it.

        Wire format: ``action cmd|search|<query>`` which the receiver's
        CommandHandler parses to ("search", "<query>").
        """
        if not self.enabled:
            return None
        clean = self.sanitize_query(query)
        if not clean:
            logger.error("[UART ERROR] Empty search query was not transmitted")
            return None
        return self.send_raw(f"action cmd|search|{clean}")

    def send_search(self, query):
        """Queue a SEARCH-with-query and return immediately to the dashboard caller."""
        if not self.enabled:
            return None
        try:
            return self._executor.submit(self.send_search_command, query)
        except Exception as exc:
            logger.exception("[UART ERROR] Could not queue search command: %s", exc)
            return None

    def shutdown(self):
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.disconnect()


uart_transmitter = UARTTransmitter.from_env()
