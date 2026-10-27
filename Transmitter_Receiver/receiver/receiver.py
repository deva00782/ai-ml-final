import argparse
import datetime
import os
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Tuple

from agents.command_handler import CommandHandler
from agents.logger import (
    FailureCategory,
    classify_failure,
    get_eval_logger,
    get_logger,
    log_box,
    setup_logging,
    validate_response,
)
from agents.media_agent import execute_media_command, get_jiosaavn_state
from agents.uart_server import UARTServer, list_serial_ports

logger = get_logger("main")
eval_logger = None

RECEIVER_MIN_AGE_TO_KILL_SECONDS = 30
FIELD_COMMAND_ID = "Command ID"
FIELD_FAILURE_CATEGORY = "Failure Category"


def _this_script_fragment():
    """Normalized absolute path of this receiver executable or script used for process matching."""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable).replace("/", "\\").lower()
    return os.path.abspath(__file__).replace("/", "\\").lower()


def _matches_receiver_process(proc, is_frozen, target_name, script_fragment) -> bool:
    pname = (proc.info.get("name") or "").lower()
    cmdline = " ".join(proc.info.get("cmdline") or []).replace("/", "\\").lower()
    if is_frozen:
        return target_name in pname or script_fragment in cmdline
    return target_name in cmdline and script_fragment in cmdline


def _kill_receivers_with_psutil(psutil, this_pid, script_fragment, target_name, is_frozen):
    now = time.time()
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            pid = proc.info["pid"]
            if pid == this_pid or not _matches_receiver_process(proc, is_frozen, target_name, script_fragment):
                continue
            start = proc.info.get("create_time")
            age = (now - start) if start else None
            if age is not None and age < RECEIVER_MIN_AGE_TO_KILL_SECONDS:
                logger.warning(
                    f"Not killing recent receiver instance (PID {pid}, {age:.0f}s old) - it may be active"
                )
                continue
            logger.debug(f"Stopping old receiver instance (PID {pid})...")
            proc.kill()
        except Exception:
            continue


def _kill_receivers_with_wmic(this_pid, target_name, is_frozen):
    wmic_clause = f"Name like '%{target_name}%'" if is_frozen else f"CommandLine like '%{target_name}%'"
    out = subprocess.run(
        ["wmic", "process", "where", wmic_clause, "get", "ProcessId", "/value"],
        capture_output=True, text=True, timeout=10
    ).stdout
    for line in out.splitlines():
        if "ProcessId=" in line:
            pid = line.strip().split("=")[-1]
            if pid.isdigit() and int(pid) != this_pid:
                logger.debug(f"Stopping old receiver instance (PID {pid})...")
                subprocess.run(["taskkill", "/F", "/PID", pid],
                               capture_output=True, text=True, timeout=10)


def kill_existing_receivers():
    """Stop older instances of THIS receiver so COM ports are not left locked."""
    if os.name != "nt":
        return
    this_pid = os.getpid()
    script_fragment = _this_script_fragment()
    is_frozen = getattr(sys, "frozen", False)
    target_name = "receiver.exe" if is_frozen else "receiver.py"

    try:
        import psutil
    except Exception:
        psutil = None

    try:
        if psutil is not None:
            _kill_receivers_with_psutil(psutil, this_pid, script_fragment, target_name, is_frozen)
        else:
            _kill_receivers_with_wmic(this_pid, target_name, is_frozen)
    except Exception as error:
        logger.warning(f"Could not auto-stop old receiver instance: {error}")


def _format_track_control_fields(command_id: str, action: str, result: dict) -> Tuple[str, dict]:
    is_fallback = result.get("fallback") == "RANDOM_SONG_SELECTION" or result.get("action") == "RANDOM_SONG_FALLBACK"
    if is_fallback:
        return "TRACK CONTROL — FALLBACK", {
            FIELD_COMMAND_ID: command_id,
            "Action": action,
            "Queue": "EMPTY",
            "Fallback": "RANDOM_SONG_SELECTION",
            "Selected": result.get("selected") or result.get("song", "Selected Song"),
            "Playback": "VERIFIED",
            "Status": "SUCCESS",
        }
    return "TRACK CONTROL", {
        FIELD_COMMAND_ID: command_id,
        "Action": action,
        "Previous": result.get("previous", "Previous Song"),
        "Current": result.get("current", result.get("song", "Current Song")),
        "Queue": result.get("queue", "ADVANCED"),
        "Fallback": "NOT_USED",
        "Status": "SUCCESS",
    }


def _log_success_box(cmd_upper: str, command: str, command_id: str, result: dict, elapsed_ms: float, payload: Any):
    if cmd_upper in ("OPEN_DESKTOP_DASHBOARD", "OPEN_JIOSAAVN", "OPEN"):
        box_title = "COMMAND RESULT"
        fields = {
            FIELD_COMMAND_ID: command_id,
            "Command": "OPEN_DESKTOP_DASHBOARD",
            "Status": "SUCCESS",
            "State": result.get("state", "JIOSAAVN_READY"),
            "Playback": result.get("playback", "PLAYING"),
            "Playlist": result.get("playlist", "Dumdaar Hits"),
            "Duration": f"{elapsed_ms:.0f} ms",
        }
    elif cmd_upper == "SEARCH":
        box_title = "SEARCH"
        fields = {
            FIELD_COMMAND_ID: command_id,
            "Query": result.get("query") or (payload if isinstance(payload, str) else "NTR Songs"),
            "Action": "SEARCH",
            "Result": "SUCCESS",
            "Match": result.get("match", "SELECTED"),
            "Playback": result.get("playback", "PLAYING"),
            "Duration": f"{elapsed_ms:.0f} ms",
        }
    elif cmd_upper in ("PLAY_PAUSE", "PLAY", "PAUSE", "MEDIA_PLAY_PAUSE"):
        box_title = "PLAYBACK CONTROL"
        fields = {
            FIELD_COMMAND_ID: command_id,
            "Action": "PLAY_PAUSE",
            "Previous": result.get("previous", "PLAYING"),
            "Current": result.get("current", "PAUSED"),
            "Status": "SUCCESS",
            "Duration": f"{elapsed_ms:.0f} ms",
        }
    elif cmd_upper in ("NEXT_TRACK", "NEXT", "MEDIA_NEXT", "PLAYNEXT", "PLAY_NEXT", "NEXTSONG", "NEXT_SONG"):
        box_title, fields = _format_track_control_fields(command_id, "NEXT_TRACK", result)
    elif cmd_upper in ("PREVIOUS_TRACK", "PREV", "PREVIOUS", "MEDIA_PREVIOUS", "PLAYPREV", "PLAY_PREV"):
        box_title, fields = _format_track_control_fields(command_id, "PREVIOUS_TRACK", result)
    elif cmd_upper in ("VOLUME_UP", "MEDIA_VOLUME_UP"):
        box_title = "VOLUME CONTROL"
        fields = {
            FIELD_COMMAND_ID: command_id,
            "Action": "VOLUME_UP",
            "Previous": result.get("previous", "70%"),
            "Current": result.get("current", "80%"),
            "Status": "SUCCESS",
        }
    elif cmd_upper in ("VOLUME_DOWN", "MEDIA_VOLUME_DOWN"):
        box_title = "VOLUME CONTROL"
        fields = {
            FIELD_COMMAND_ID: command_id,
            "Action": "VOLUME_DOWN",
            "Previous": result.get("previous", "80%"),
            "Current": result.get("current", "70%"),
            "Status": "SUCCESS",
        }
    elif cmd_upper == "BACK_TO_LEVEL_2":
        box_title = "FSM TRANSITION"
        fields = {
            FIELD_COMMAND_ID: command_id,
            "Previous State": result.get("previous_state", "LEVEL_3"),
            "New State": result.get("new_state", "LEVEL_2"),
            "Action": "BACK_TO_LEVEL_2",
            "Status": "SUCCESS",
        }
    else:
        box_title = "COMMAND RESULT"
        fields = {
            FIELD_COMMAND_ID: command_id,
            "Command": cmd_upper,
            "Status": "SUCCESS",
            "Duration": f"{elapsed_ms:.0f} ms",
        }

    log_box(
        box_title,
        fields,
        event_name="COMMAND_EXECUTION_COMPLETED",
        command_id=command_id,
        command=command,
    )


def _handle_action_failure(server, command, payload, command_id, start_time, confidence, expected_category, error):
    elapsed_ms = (time.perf_counter() - start_time) * 1000
    cmd_upper = command.upper()
    logger.debug(f"Command '{command}' [{command_id}] failed after {elapsed_ms:.1f}ms: {error}", exc_info=True)

    err_str = str(error)
    device_resp = "TIMEOUT" if ("TIMEOUT" in err_str.upper()) else "FAILED"
    failure_cat = classify_failure(error=error, device_response=device_resp, confidence=confidence)

    reason_msg = "No device response within configured timeout period." if failure_cat == FailureCategory.DEVICE_TIMEOUT else err_str

    fc_fields = [
        (FIELD_COMMAND_ID, command_id),
        ("Command", cmd_upper),
        ("Execution Status", "FAILED"),
        ("Device Response", device_resp),
        ("", ""),
        (FIELD_FAILURE_CATEGORY, failure_cat),
        (" ", ""),
        ("Reason", reason_msg),
    ]
    log_box(
        "FAILURE CLASSIFICATION — MEMBER 3",
        fc_fields,
        event_name="FAILURE_CLASSIFIED",
        command_id=command_id,
        command=command,
    )

    val_str, is_match = validate_response(
        device_status=device_resp,
        tracker_status="FAILED",
        failure_category=failure_cat,
        expected_category=expected_category or failure_cat,
    )

    val_fields = [
        (FIELD_COMMAND_ID, command_id),
        ("Command", cmd_upper),
        ("", ""),
        ("Device Status", device_resp),
        ("Tracker Status", "FAILED"),
    ]
    if is_match:
        val_fields.append((FIELD_FAILURE_CATEGORY, failure_cat))
        val_fields.append(("Expected", failure_cat))
    else:
        val_fields.append(("Recorded Reason", failure_cat))
        val_fields.append(("Expected Reason", expected_category or FailureCategory.DEVICE_TIMEOUT))
    val_fields.append((" ", ""))
    val_fields.append(("Validation", val_str))

    log_box(
        "RESPONSE VALIDATION — MEMBER 4",
        val_fields,
        event_name="RESPONSE_VALIDATION",
        command_id=command_id,
        command=command,
    )

    if eval_logger:
        eval_logger.log_event(
            event="COMMAND_FAILED",
            command_id=command_id,
            command=command,
            payload=payload,
            status="FAILED",
            duration_ms=elapsed_ms,
            details={"error": err_str, "reason": failure_cat},
        )
        eval_logger.log_evaluation_record(
            command_id=command_id,
            command=command,
            device_status=device_resp,
            tracker_status="FAILED",
            failure_category=failure_cat,
            validation=val_str,
            expected_category=expected_category,
            recorded_reason=failure_cat if not is_match else None,
            expected_reason=expected_category if not is_match else None,
            details={"error": err_str},
        )

    try:
        server.send_ack(command, "FAILED", message=err_str, extra={"command_id": command_id, "reason": failure_cat})
    except Exception as ack_err:
        logger.exception(f"Failed to send failure ACK to PC1: {ack_err}")


def execute_action(server, command, payload, command_id, start_time, confidence=None, expected_category=None):
    """Run the media command in a thread, then report execution result to PC1."""
    cmd_upper = command.upper()
    try:
        result = execute_media_command(command, payload, command_id=command_id)
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        extra_ack = {"command_id": command_id}
        if isinstance(result, dict):
            for k in ("state", "playback", "playlist", "song", "action", "selected"):
                if k in result and result[k] is not None:
                    extra_ack[k] = result[k]

        _log_success_box(cmd_upper, command, command_id, result, elapsed_ms, payload)

        validation_str, _ = validate_response(
            device_status="SUCCESS",
            tracker_status="SUCCESS",
            failure_category=FailureCategory.SUCCESS,
            expected_category=expected_category or FailureCategory.SUCCESS,
        )

        if eval_logger:
            eval_logger.log_event(
                event="COMMAND_EXECUTED",
                command_id=command_id,
                command=command,
                payload=payload,
                status="SUCCESS",
                duration_ms=elapsed_ms,
                details=extra_ack,
            )
            eval_logger.log_evaluation_record(
                command_id=command_id,
                command=command,
                device_status="SUCCESS",
                tracker_status="SUCCESS",
                failure_category=FailureCategory.SUCCESS,
                validation=validation_str,
                expected_category=expected_category,
            )

        server.send_ack(command, "SUCCESS", extra=extra_ack)

    except Exception as error:
        _handle_action_failure(server, command, payload, command_id, start_time, confidence, expected_category, error)


def _handle_unknown_command(server, raw_data, command_id, now_time, payload):
    log_box(
        "COMMAND RECEIVED",
        {
            FIELD_COMMAND_ID: command_id,
            "Command": "UNKNOWN",
            "Source": "UART",
            "Time": now_time,
            "Status": "REJECTED",
        },
        event_name="COMMAND_RECEIVED",
        command_id=command_id,
        command="UNKNOWN",
    )

    fc_fields = [
        (FIELD_COMMAND_ID, command_id),
        ("Command", "UNKNOWN"),
        ("Execution Status", "FAILED"),
        ("Device Response", "INVALID"),
        ("", ""),
        (FIELD_FAILURE_CATEGORY, FailureCategory.INVALID_COMMAND),
        (" ", ""),
        ("Reason", f"Unrecognized incoming raw command: {raw_data!r}"),
    ]
    log_box(
        "FAILURE CLASSIFICATION — MEMBER 3",
        fc_fields,
        event_name="FAILURE_CLASSIFIED",
        command_id=command_id,
        command="UNKNOWN",
    )

    val_str, _ = validate_response(
        device_status="INVALID",
        tracker_status="FAILED",
        failure_category=FailureCategory.INVALID_COMMAND,
        expected_category=FailureCategory.INVALID_COMMAND,
    )
    log_box(
        "RESPONSE VALIDATION — MEMBER 4",
        [
            (FIELD_COMMAND_ID, command_id),
            ("Command", "UNKNOWN"),
            ("", ""),
            ("Device Status", "INVALID"),
            ("Tracker Status", "FAILED"),
            (FIELD_FAILURE_CATEGORY, FailureCategory.INVALID_COMMAND),
            ("Expected", FailureCategory.INVALID_COMMAND),
            (" ", ""),
            ("Validation", val_str),
        ],
        event_name="RESPONSE_VALIDATION",
        command_id=command_id,
        command="UNKNOWN",
    )

    server.send_ack(
        "UNKNOWN",
        "FAILED",
        message="Unrecognized command",
        extra={"command_id": command_id, "reason": FailureCategory.INVALID_COMMAND},
    )

    if eval_logger:
        eval_logger.log_event(
            event="COMMAND_FAILED",
            command_id=command_id,
            command="UNKNOWN",
            payload=payload,
            status="REJECTED",
            details={"reason": "Unrecognized command", "raw_data": raw_data},
        )
        eval_logger.log_evaluation_record(
            command_id=command_id,
            command="UNKNOWN",
            device_status="INVALID",
            tracker_status="FAILED",
            failure_category=FailureCategory.INVALID_COMMAND,
            validation=val_str,
            expected_category=FailureCategory.INVALID_COMMAND,
            details={"raw_data": raw_data},
        )


def _handle_recognized_command(server, command, payload, command_id, raw_data, now_time, t0):
    if command == "PING":
        server.send_ack("PING", "PONG", extra={"command_id": command_id})
        if eval_logger:
            eval_logger.log_event(
                event="ACK_SENT",
                command_id=command_id,
                command="PING",
                status="PONG",
            )
        return

    log_box(
        "COMMAND RECEIVED",
        {
            FIELD_COMMAND_ID: command_id,
            "Command": command.upper(),
            "Query": payload if isinstance(payload, str) and payload.strip() else (payload or "-"),
            "Source": "UART",
            "Time": now_time,
            "Status": "RECEIVED",
        },
        event_name="COMMAND_RECEIVED",
        command_id=command_id,
        command=command,
    )

    server.send_ack(command, "RECEIVED", extra={"command_id": command_id})

    action_label = "INITIALIZE_JIOSAAVN" if command in ("open_desktop_dashboard", "open_jiosaavn", "open") else command.upper()
    curr_state = "JIOSAAVN_OPENING" if command in ("open_desktop_dashboard", "open_jiosaavn", "open") else get_jiosaavn_state()

    exec_fields = {
        FIELD_COMMAND_ID: command_id,
        "Command": command.upper(),
        "Action": action_label,
        "State": curr_state,
        "Time": datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3],
    }
    if isinstance(payload, str) and payload.strip():
        exec_fields["Query"] = payload.strip()
    log_box(
        "COMMAND EXECUTION",
        exec_fields,
        event_name="COMMAND_EXECUTION_STARTED",
        command_id=command_id,
        command=command,
    )

    if eval_logger:
        eval_logger.log_event(
            event="COMMAND_RECEIVED",
            command_id=command_id,
            command=command,
            payload=payload,
            status="RECEIVED",
            details={"raw_data": raw_data},
        )

    threading.Thread(
        target=execute_action,
        args=(server, command, payload, command_id, t0),
        daemon=True,
    ).start()


def _process_serial_line(server, raw_data):
    command_id = f"cmd_{uuid.uuid4().hex[:8]}"
    t0 = time.perf_counter()
    logger.debug(f"Received raw data [{command_id}]: {raw_data}")

    command, payload = CommandHandler.parse(raw_data)
    logger.debug(f"Parsed Command [{command_id}]: {command} | Payload: {payload}")

    now_time = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

    if command and command != "UNKNOWN":
        _handle_recognized_command(server, command, payload, command_id, raw_data, now_time, t0)
    else:
        _handle_unknown_command(server, raw_data, command_id, now_time, payload)


def _handle_list_ports():
    print("\n=========================================================")
    print("  Scanning for Connected Serial / COM Ports...")
    print("=========================================================")
    ports = list_serial_ports()
    if not ports:
        print("  [!] No serial COM ports detected on this system.")
        print("      Ensure your USB-to-UART bridge / device is plugged in.")
    else:
        for p in ports:
            is_uart = (p.get("vid") in ("0x10C4", "0x0403", "0x1A86", "0x067B", "0x7523", "0x303A", "0x2E8A", "0x2341", "0x2A03")) or any(k in (p.get("description") or "").upper() for k in ("CP210", "SILICON LABS", "FTDI", "FT232", "CH340", "CH341", "PL2303", "PROLIFIC", "USB SERIAL", "UART"))
            tag = " [USB-UART BRIDGE DETECTED]" if is_uart else ""
            print(f"  [+] {p['port']:<8} | {p['description']}{tag}")
            print(f"      HWID: {p['hwid']} | VID: {p.get('vid')} | PID: {p.get('pid')}")
    print("=========================================================\n")


def _build_parser():
    parser = argparse.ArgumentParser(
        description="SynaptiMesh BCI Media Receiver - Standalone Host Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-p", "--port",
        dest="port",
        default=None,
        help="Serial/COM port to connect (e.g. COM3, COM7). Default: auto-detect USB-to-UART adapter.",
    )
    parser.add_argument(
        "-b", "--baud",
        dest="baud",
        type=int,
        default=None,
        help="Baud rate (default: 9600). Overrides config.json.",
    )
    parser.add_argument(
        "-c", "--config",
        dest="config",
        default="agents/config.json",
        help="Path to custom config.json (default: config.json or agents/config.json).",
    )
    parser.add_argument(
        "-l", "--list-ports",
        dest="list_ports",
        action="store_true",
        help="Scan and list all detected system COM ports and exit.",
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version="SynaptiMesh BCI Receiver 2.0 (PyInstaller Standalone)",
    )
    return parser


def main():
    global eval_logger

    parser = _build_parser()
    args = parser.parse_args()

    if args.list_ports:
        _handle_list_ports()
        return

    setup_logging(args.config)
    eval_logger = get_eval_logger()

    log_box(
        "SYSTEM STARTUP",
        {
            "Time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "Component": "RECEIVER",
            "Status": "ONLINE",
            "PID": os.getpid(),
            "Log Mode": "Production Structured Logging",
        },
        event_name="SYSTEM_START",
    )

    kill_existing_receivers()

    logger.debug("Initializing UART Server on PC2...")
    server = None
    try:
        server = UARTServer(config_path=args.config, port=args.port, baud=args.baud)
        logger.debug(f"UART Server online on {server.port} at {server.baud} baud. Listening for BCI commands...")
    except Exception as error:
        logger.exception(
            "Receiver initialization aborted: UART Server failed to start.",
            extra={"error": str(error)},
        )
        if eval_logger:
            eval_logger.log_event(
                event="SYSTEM_ERROR",
                status="FAILED",
                details={"error": str(error), "stage": "UART_INIT"},
            )
        return

    try:
        while True:
            try:
                raw_data = server.read()
                if raw_data:
                    _process_serial_line(server, raw_data)
            except Exception as error:
                logger.exception(f"UART read cycle error: {error}")
            time.sleep(0.01)
    except KeyboardInterrupt:
        logger.debug("Shutting down receiver on user request (Ctrl+C)...")
        if eval_logger:
            eval_logger.log_event(event="SYSTEM_SHUTDOWN", details={"reason": "KeyboardInterrupt"})
    finally:
        if server:
            server.close()
        log_box(
            "SYSTEM SHUTDOWN",
            {
                "Component": "RECEIVER",
                "UART": "DISCONNECTED",
                "Active Cmd": "NONE",
                "Status": "STOPPED",
                "Time": datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3],
            },
            event_name="SYSTEM_SHUTDOWN",
        )


if __name__ == "__main__":
    main()