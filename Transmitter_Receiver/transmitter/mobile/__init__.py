"""
NeuroDrive Mobile IoT & Cloud Sync Package.
Provides MQTT communication with Android and iOS devices, and cloud synchronization
with AWS RDS PostgreSQL via the SynaptiMesh Sync API.
"""

import sys

# Ensure services aliases are registered before submodules execute
sys.modules.setdefault("services", sys.modules[__name__])
sys.modules.setdefault("mobile.services", sys.modules[__name__])

# Submodule imports
from . import config
sys.modules.setdefault("config.config", config)
sys.modules.setdefault("mobile.config", config)
sys.modules.setdefault("mobile_dashboard.mobile_plugin.config", config)

from . import socket_events
sys.modules.setdefault("events", socket_events)
sys.modules.setdefault("events.socket_events", socket_events)
sys.modules.setdefault("mobile.events", socket_events)
sys.modules.setdefault("mobile.events.socket_events", socket_events)
sys.modules.setdefault("services.logger_service", socket_events)
sys.modules.setdefault("mobile.services.logger_service", socket_events)

from . import sync_api_client
sys.modules.setdefault("services.sync_api_client", sync_api_client)
sys.modules.setdefault("mobile.services.sync_api_client", sync_api_client)
sys.modules.setdefault("sync_api_client", sync_api_client)

from . import device_db_service
sys.modules.setdefault("services.device_db_service", device_db_service)
sys.modules.setdefault("mobile.services.device_db_service", device_db_service)

from . import mqtt_service
sys.modules.setdefault("services.mqtt_service", mqtt_service)
sys.modules.setdefault("mobile.services.mqtt_service", mqtt_service)

from .mqtt_service import MQTTService
from .socket_events import setup_logger, register_socket_events

__version__ = "1.0.0"
__author__ = "NeuroDrive Team"

# Package-level backward compatibility aliases
sys.modules.setdefault("mobile_dashboard", sys.modules[__name__])
sys.modules.setdefault("mobile_dashboard.mobile_plugin", sys.modules[__name__])
sys.modules.setdefault("mobile_dashboard.mobile_plugin.services", sys.modules[__name__])
sys.modules.setdefault("mobile_dashboard.mobile_plugin.events", socket_events)
sys.modules.setdefault("mobile.services", sys.modules[__name__])
sys.modules.setdefault("services", sys.modules[__name__])

__all__ = [
    "MQTTService",
    "setup_logger",
    "register_socket_events",
    "config",
    "mqtt_service",
    "sync_api_client",
    "device_db_service",
    "socket_events",
]
