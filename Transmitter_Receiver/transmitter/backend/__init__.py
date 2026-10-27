"""
Backend package for SynaptiMesh Transmitter.
Contains device registry, command routing, PostgreSQL persistence, hardware UART transceiver, and boxed logging.
"""

import sys

try:
    from . import boxed_logger
    sys.modules.setdefault("boxed_logger", boxed_logger)
except ImportError:
    pass

try:
    from .uart_transmitter import uart_transmitter, UARTTransmitter
    sys.modules.setdefault("uart_transmitter", sys.modules.get(f"{__name__}.uart_transmitter"))
except ImportError:
    pass

from .device_manager import DeviceManager, device_manager, StateManager
from .command_router import CommandRouter, command_router
from .postgres_store import PostgresStore

__all__ = [
    "DeviceManager",
    "device_manager",
    "StateManager",
    "CommandRouter",
    "command_router",
    "PostgresStore",
    "uart_transmitter",
    "UARTTransmitter",
    "boxed_logger",
]
