# SynaptiMesh Transmitter (PC1)

The transmitter is the **control side** of the SynaptiMesh BCI system. It turns brain-computer-interface
mental commands (or dashboard test buttons) into media-control actions and delivers each action to exactly
one target: the **phone over MQTT**, **this PC's desktop player over Selenium/OS keys**, or the **second
PC (receiver) over USB-to-TTL UART**.

Single server: `python app.py` → unified UI at `http://localhost:8000/dashboard`.

## How it works

```
BCI headset / dashboard buttons
  -> POST /api/bci/command {"command":"LEFT","confidence":0.95}
  -> command_system/command_window.py   (4.0 s window, dedup, ordered doubles)
  -> command_system/fsm_controller.py   (state machine, decides target_device)
  -> command_system/command_mapper.py   (logical action -> canonical action)
  -> backend/command_router.py          (single-target dispatch)
       -> Mobile   : MQTT publish, topic bci/rohan/commands (broker.emqx.io:1883)
       -> Desktop  : desktop_dashboard/desktop_modules (Selenium / OS keys)
       -> Receiver : uart_transmitter.send_action()  (async, best-effort)
```

### FSM hierarchy (`command_system/states.py` — single source of truth)

- **Level 1** `DOMAIN_SELECTION`: `RIGHT` → Level 2
- **Level 2** `SUB_MASTER_DASHBOARD`: `PUSH` → Mobile, `PULL` → Desktop, `RIGHT` → back to L1
- **Level 3** `MOBILE/DESKTOP_DASHBOARD_ACTIVE`: `LEFT` play/pause, `PUSH` next, `PULL` previous, `RIGHT` search
- **Ordered doubles** (within the 4 s window): `RIGHT+PUSH` volume up, `RIGHT+PULL` volume down,
  `PUSH+RIGHT` back to L2, `PUSH+LEFT` back to L1

Config: `COMMAND_WINDOW_SECONDS=4.0`, `CONFIDENCE_THRESHOLD=0.35`, volume debounce 2 s.
`NEUTRAL` is ignored; repeats inside a window are suppressed; unknown primitives are rejected.

## Project structure

```
transmitter/
  app.py                      Flask + SocketIO server (UI + REST API)
  uart_transmitter.py         Async UART sender (from_env() singleton)
  test_uart.py                UART diagnostic CLI (--port/--baudrate/--command/--list-ports)
  replay_bci.py               Replay recorded sessions (realtime/fast/dry-run)
  desktop_controller.py       Desktop REST backend (registered by app.py)
  boxed_logger.py             Structured logging helper
  command_system/             BCI -> window -> FSM -> mapper
    fsm_controller.py, states.py, command_mapper.py, command_parser.py,
    command_window.py, cortex_bridge.py, mock_bci.py, bci_recorder.py, bci_replay.py
  backend/                    device_manager / state_manager / command_router
  dashboard/                  The served UI (templates + static) -> /dashboard
  desktop_dashboard/          Desktop player backend (desktop_modules/) + legacy templates
  mobile_dashboard/           Mobile backend (mobile_plugin/) + templates
  master_dashboard/           Legacy UI (ChoiceLoader fallback only)
  bci_data/                   Recorded sessions (raw/ immutable, replay/) — see bci_data/README.md
  tests/                      test_bci_recording_replay.py
  requirements.txt
```

## Requirements

- Python 3.10+ (3.13/3.14 used in development), Windows 10/11 for volume/media keys
- `pip install -r requirements.txt` — Flask, Flask-SocketIO, paho-mqtt, selenium, requests,
  PyAutoGUI, websocket-client, pyserial, python-dotenv, psutil (+ comtypes/pycaw/winsdk on Windows)
- Hardware (only for the paths you use): Emotiv Cortex headset via local Cortex service
  (`wss://localhost:6868`) for live BCI; CP210x USB-to-TTL adapter for UART; network for MQTT

## Installation / setup

```powershell
cd transmitter
pip install -r requirements.txt
```

Optional: copy nothing — there is no config file. Everything is environment variables with safe defaults.

## How to run

```powershell
cd transmitter
python app.py
# -> http://localhost:8000/dashboard
```

Secondary entry points:

```powershell
python test_uart.py --port COM3 --command "action cmd|TEST"   # UART diagnostics
python replay_bci.py bci_data/raw/session_001.json            # realtime replay
python replay_bci.py bci_data/raw/session_001.json --fast --dry-run
python -m pytest tests/ 2>$null; python tests/test_bci_recording_replay.py
```

Without a BCI headset, use the dashboard BCI buttons — they post primitives through the same
4 s window as live data. No code changes needed.

## Configuration (environment variables, defaults shown)

```text
UNIFIED_PORT=8000                  # server port (fallback MASTER_HUB_PORT, then 8000)
SECRET_KEY=unified_dashboard_secret# Flask sessions; set a private value in production
CORTEX_CLIENT_ID=                  # set both to enable the live Emotiv bridge,
CORTEX_CLIENT_SECRET=              # otherwise dashboard test buttons are the input
BCI_RECORDING=0                    # set to 1/true to auto-record sessions to bci_data/raw/
UART_ENABLED=true                  # false = dashboard runs with zero UART side effects
UART_PORT=COM3                     # auto-detects Silicon Labs CP210x if unset/wrong
UART_BAUDRATE=9600                 # must match the receiver (9600)
UART_TIMEOUT=1.0
UART_ACK_ENABLED=false             # true only if the receiver echoes a reply line
UART_TERMINATOR=\n
```

UART is a best-effort side effect: serial errors and ACK timeouts are logged (`[UART RX]`) without
interrupting the dashboard. Wire payload: `action cmd|<canonical_action>\n`. Wiring: TX→RX, RX→TX, GND→GND.

## Communication with the receiver

- Transport: USB-to-TTL UART (Silicon Labs CP210x), **9600 baud both sides**.
- Each desktop-targeted canonical action (`play_pause`, `next_track`, `previous_track`, `volume_up`,
  `volume_down`, `search`, …) is sent as one `action cmd|<action>` line; mobile-targeted actions go to
  MQTT instead and never touch UART.
- The receiver replies `RECEIVED` then `SUCCESS`/`FAILED`; with `UART_ACK_ENABLED=false` (default)
  replies are only logged, never waited on.

## Expected inputs / outputs

- In: Emotiv mental-command primitives (`LEFT/RIGHT/PUSH/PULL`, confidence 0–1) via Cortex bridge or
  `POST /api/bci/command`; direct debug via `POST /api/fsm/command`; volume via `POST /api/volume`.
- Out: MQTT command JSON on `bci/rohan/commands`, local desktop player actions, UART lines for the receiver.

## Troubleshooting

| Symptom | Check |
|---|---|
| Dashboard unreachable | `UNIFIED_PORT` in use? Console shows the actual port; open `http://localhost:<port>/dashboard` |
| No live BCI | Both `CORTEX_CLIENT_ID` and `CORTEX_CLIENT_SECRET` must be set; else test buttons are the input by design |
| UART errors / no receiver reply | `UART_PORT`, crossed TX/RX, GND, baud 9600 both sides; `python test_uart.py --list-ports`; loopback TX→RX to prove the adapter |
| winrt/media-status log spam | Fixed: logged once per process (`os_operations.py`) |
| cp1252/emoji console crash | Fixed: UTF-8 stdout/stderr reconfigure (`start_chrome.py`) + `errors="replace"` (`app.py`) |
| Mobile gets nothing | Broker `broker.emqx.io:1883` reachable? Topics `bci/rohan/#`; check `mqtt_service` logs |

More history: `REPORT.md` (pipeline + session fixes), `bci_data/README.md` (recording/replay format).

## Logs, venv, deployment notes

- Runtime log: `desktop_dashboard/desktop_modules/system_execution.log` (auto-created, ~MBs). **Kept
  on disk, never committed** (see `.gitignore`).
- No venv is bundled with the transmitter — create one if you like (`python -m venv venv`), it is git-ignored.
- Do not commit `.env` files or real `CORTEX_*` credentials; `SECRET_KEY` has a dev default — override it
  via environment in any shared/production deployment.
