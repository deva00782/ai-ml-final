import datetime
import json
import os
import sys
import threading
import serial
import serial.tools.list_ports
from agents.logger import get_logger, get_base_dir, log_box

logger = get_logger("uart")

CONFIG_FILE_NAME = "config.json"
SILICON_LABS_VID = 0x10C4
COMMON_UART_VIDS = {
    0x10C4,  # Silicon Labs CP210x
    0x0403,  # FTDI
    0x1A86,  # WCH CH340 / CH341 / CH343 / CH9102
    0x067B,  # Prolific PL2303
    0x7523,  # QinHeng
    0x303A,  # Espressif ESP32 USB
    0x2E8A,  # Raspberry Pi RP2040
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


def list_serial_ports():
    """Returns a list of dictionaries with detected serial ports and their details."""
    ports = []
    try:
        for p in serial.tools.list_ports.comports():
            vid_hex = f"0x{p.vid:04X}" if p.vid is not None else None
            pid_hex = f"0x{p.pid:04X}" if p.pid is not None else None
            ports.append({
                "port": p.device,
                "description": p.description,
                "hwid": p.hwid,
                "vid": vid_hex,
                "pid": pid_hex,
                "serial_number": getattr(p, "serial_number", None) or "N/A",
            })
    except Exception as e:
        logger.warning(f"Failed to query system serial ports: {e}")
    return ports


def _is_bluetooth_port(p) -> bool:
    """Check if port is a virtual Bluetooth serial link."""
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


def _pick_best_candidate(candidates):
    """Picks candidate matching known CP210x PIDs, or defaults to first."""
    for c in candidates:
        if c.get("pid") and c["pid"] != "Unknown":
            try:
                if int(c["pid"], 16) in KNOWN_CP210X_PIDS:
                    return c
            except ValueError:
                pass
    return candidates[0]


def _port_to_dict(p):
    vid_hex = f"0x{p.vid:04X}" if p.vid is not None else "Unknown"
    pid_hex = f"0x{p.pid:04X}" if p.pid is not None else "Unknown"
    return {
        "port": p.device,
        "description": p.description,
        "vid": vid_hex,
        "pid": pid_hex,
        "hwid": p.hwid,
        "serial_number": getattr(p, "serial_number", None) or "N/A",
    }


def _find_fallback_uart_port():
    for p in serial.tools.list_ports.comports():
        if _is_bluetooth_port(p):
            continue
        desc_or_hwid = ((p.description or "") + " " + (p.hwid or "")).upper()
        if "USB" in desc_or_hwid:
            return _port_to_dict(p)
    return None


def find_uart_port():
    """Auto-detects any connected USB-to-UART bridge adapter (CP210x, FTDI, CH340, PL2303, CDC, etc.).
    Returns dict with details ('port', 'description', 'vid', 'pid', 'hwid', 'serial_number') or None.
    """
    try:
        candidates = [_port_to_dict(p) for p in serial.tools.list_ports.comports() if _is_uart_port(p)]
        if candidates:
            return _pick_best_candidate(candidates)

        return _find_fallback_uart_port()
    except Exception as e:
        logger.warning(f"Error while scanning for UART adapter: {e}")
    return None


def find_silicon_labs_port():
    """Backward-compatible alias for find_uart_port()."""
    return find_uart_port()


def _load_uart_config(config_path):
    """Loads configuration options from candidate files."""
    base_dir = get_base_dir()
    config = {"serial_port": "AUTO", "baud_rate": 9600, "timeout": 1}
    candidates = []
    if config_path:
        candidates.append(config_path if os.path.isabs(config_path) else os.path.join(base_dir, config_path))
    candidates.append(os.path.join(base_dir, CONFIG_FILE_NAME))
    candidates.append(os.path.join(base_dir, "agents", CONFIG_FILE_NAME))

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "agents", CONFIG_FILE_NAME))
        candidates.append(os.path.join(sys._MEIPASS, CONFIG_FILE_NAME))

    for cpath in candidates:
        if os.path.isfile(cpath):
            try:
                with open(cpath, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    config.update(loaded)
                    logger.info(f"Loaded UART configuration from {cpath}")
                    break
            except Exception as err:
                logger.warning(f"Error reading config from {cpath}: {err}")
    return config


def _resolve_port(port, config):
    """Resolves target port, source description, and detected adapter dynamically."""
    if port and str(port).strip().upper() not in ("", "AUTO", "NONE"):
        return str(port).strip(), "explicit argument", None

    for env_var in ("RECEIVER_PORT", "SERIAL_PORT", "UART_PORT"):
        env_val = os.environ.get(env_var)
        if env_val and env_val.strip().upper() not in ("", "AUTO", "NONE"):
            return env_val.strip(), f"{env_var} env", None

    detected_adapter = find_uart_port()
    if detected_adapter:
        return detected_adapter["port"], f"Auto-detected USB-UART ({detected_adapter['description']})", detected_adapter

    config_port = config.get("serial_port")
    if config_port and str(config_port).strip().upper() not in ("", "AUTO", "NONE"):
        return str(config_port).strip(), f"{CONFIG_FILE_NAME} fallback", None

    return None, None, None


class UARTServer:
    def __init__(self, config_path="agents/config.json", port=None, baud=None, timeout=None):
        self.config = _load_uart_config(config_path)

        target_port, port_source, detected_adapter = _resolve_port(port, self.config)

        if not target_port:
            available_ports = [p for p in list_serial_ports() if "BTHENUM" not in (p["hwid"] or "").upper()]
            ports_summary = "\n".join(
                [f"  - {p['port']}: {p['description']} (HWID: {p['hwid']})" for p in available_ports]
            ) if available_ports else "  (No physical serial ports detected on system)"
            err_msg = (
                f"No USB-to-UART adapter detected!\n"
                f"Detected system ports:\n{ports_summary}\n"
                f"Remediation: Connect USB-to-UART adapter or pass an explicit port via --port <PORT> or {CONFIG_FILE_NAME}."
            )
            logger.error(err_msg)
            raise serial.SerialException(err_msg)

        if detected_adapter and target_port == detected_adapter["port"]:
            logger.info(f"USB-UART adapter detected: {detected_adapter['port']}")
            logger.info(
                f"UART adapter detected:\n"
                f"  Port: {detected_adapter['port']}\n"
                f"  Description: {detected_adapter['description']}\n"
                f"  VID: {detected_adapter['vid']}\n"
                f"  PID: {detected_adapter['pid']}\n"
                f"  Serial: {detected_adapter['serial_number']}"
            )
        else:
            logger.info(f"Using UART serial port: {target_port} (source: {port_source})")

        self.port = str(target_port).strip()
        self.baud = self._parse_baud(baud)
        self.timeout = self._parse_timeout(timeout)

        logger.debug(
            f"Connecting to serial port {self.port} at {self.baud} baud (timeout={self.timeout}s)...",
            extra={"port": self.port, "baud_rate": self.baud, "timeout": self.timeout, "source": port_source},
        )
        self._open_connection(detected_adapter)
        self._write_lock = threading.Lock()

    def _parse_baud(self, baud) -> int:
        active_baud = baud or os.environ.get("RECEIVER_BAUD") or os.environ.get("SERIAL_BAUD") or self.config.get("baud_rate", 9600)
        try:
            return int(active_baud)
        except (ValueError, TypeError):
            return 9600

    def _parse_timeout(self, timeout) -> float:
        active_timeout = timeout if timeout is not None else self.config.get("timeout", 1)
        try:
            return float(active_timeout)
        except (ValueError, TypeError):
            return 1.0

    def _open_connection(self, detected_adapter):
        try:
            self.ser = serial.Serial(
                self.port,
                self.baud,
                timeout=self.timeout,
                rtscts=False,
                dsrdtr=False,
                xonxoff=False,
            )
            self.ser.dtr = False
            self.ser.rts = False
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            logger.debug(
                f"Serial port {self.port} successfully opened and active.",
                extra={"port": self.port, "baud_rate": self.baud},
            )
            adapter_desc = (detected_adapter.get("description") if detected_adapter else None) or "Silicon Labs CP210x USB to UART Bridge"
            log_box(
                "UART CONNECTION",
                {
                    "Component": "UART",
                    "Port": self.port,
                    "Baud Rate": self.baud,
                    "Adapter": adapter_desc,
                    "Status": "CONNECTED",
                    "Time": datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3],
                },
                event_name="UART_CONNECTED",
            )
        except serial.SerialException as error:
            available_ports = [
                f"{p['port']} ({p['description']})" for p in list_serial_ports()
            ]
            logger.exception(
                f"Failed to open serial port '{self.port}': {error}",
                extra={
                    "target_port": self.port,
                    "baud_rate": self.baud,
                    "detected_system_ports": available_ports or ["None detected"],
                    "remediation": f"Connect USB-to-TTL adapter, edit {CONFIG_FILE_NAME}, or start receiver with: --port <PORT>",
                },
            )
            raise

    def read(self):
        """Reads non-empty incoming line from serial."""
        if self.ser.in_waiting > 0:
            raw_line = self.ser.readline().decode('utf-8', errors='ignore').strip()
            if raw_line:
                logger.debug(f"Received serial bytes -> {raw_line}")
                return raw_line
        return None

    def _build_ack_fields(self, action_name, result, extra):
        ack_cmd_id = extra.get("command_id") if (extra and isinstance(extra, dict)) else None
        ack_fields = {}
        if ack_cmd_id:
            ack_fields["Command ID"] = ack_cmd_id
        ack_fields["Command"] = action_name.upper()
        ack_fields["Result"] = result
        if extra and isinstance(extra, dict):
            for key in ("state", "playback", "reason"):
                val = extra.get(key)
                if val:
                    ack_fields[key.capitalize()] = val
        ack_fields["ACK"] = "SENT"
        return ack_fields, ack_cmd_id

    def send_ack(self, action_name, result="SUCCESS", message=None, extra=None):
        """Send a JSON acknowledgement packet back to PC1.

        result: "PONG" (ping), "RECEIVED", "SUCCESS" or "FAILED".
        extra: optional dictionary with fields such as 'state', 'playback', 'reason'.
        """
        response = {
            "status": "ACK",
            "command": action_name,
            "result": result,
        }
        if message:
            response["message"] = str(message)
        if extra and isinstance(extra, dict):
            for k, v in extra.items():
                if v is not None:
                    response[k] = v
        packet = json.dumps(response) + "\n"
        with self._write_lock:
            self.ser.write(packet.encode("utf-8"))
            self.ser.flush()

        ack_fields, ack_cmd_id = self._build_ack_fields(action_name, result, extra)
        log_box(
            "UART ACKNOWLEDGEMENT",
            ack_fields,
            event_name="ACK_SENT",
            command_id=ack_cmd_id,
            command=action_name,
        )
        logger.debug(f"[UART ACK Packet] {packet.strip()}")

    def close(self):
        if hasattr(self, "ser") and self.ser.is_open:
            logger.debug("Closing serial port connection...")
            self.ser.close()
            logger.debug("Serial port closed.")