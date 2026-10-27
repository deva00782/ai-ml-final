"""PostgreSQL integration layer for SynaptiMesh BCI.
===================================================
Provides persistent storage for registered mobile/BCI devices using AWS RDS PostgreSQL.
Guarantees device persistence across transmitter server restarts.

Schema:
    CREATE TABLE IF NOT EXISTS aiml (
        device_id   TEXT PRIMARY KEY,
        username    TEXT UNIQUE NOT NULL,
        device_type TEXT
    );
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

try:
    from mobile import device_db_service as db_service
except ImportError:
    try:
        from services import device_db_service as db_service
    except ImportError:
        import device_db_service as db_service

logger = logging.getLogger(__name__)


class PostgresStore:
    """PostgreSQL data access layer for device persistence and status."""

    def __init__(self):
        self._initialized = False

    def initialize(self) -> bool:
        """Initialize and verify the aiml schema on AWS RDS PostgreSQL."""
        try:
            self._initialized = db_service.ensure_schema()
            return self._initialized
        except Exception as e:
            logger.warning(f"[POSTGRES] Failed to initialize schema: {e}")
            return False

    def close(self):
        """Close database resources (no-op for connection-per-call pattern)."""
        self._initialized = False

    def health_check(self) -> Dict[str, Any]:
        """Check database connectivity and return status."""
        try:
            ok = db_service.ensure_schema()
            return {"healthy": ok, "database": db_service.DB_NAME, "host": db_service.DB_HOST}
        except Exception as e:
            return {"healthy": False, "error": str(e)}

    # ----------------------------------------------------------------------
    # Device Registration (Table: aiml)
    # ----------------------------------------------------------------------
    def upsert_device(
        self,
        device_id: str,
        username: str,
        device_type: str = "android",
        is_active: bool = True,
    ) -> bool:
        """Insert or update a device in AWS RDS PostgreSQL."""
        res = db_service.upsert_device(device_id, username, device_type)
        return res is not None

    def update_device_heartbeat(self, device_id: str) -> bool:
        """Heartbeat no-op (aiml schema has no heartbeat column)."""
        return True

    def set_device_active(self, device_id: str, is_active: bool) -> bool:
        """Active status no-op (handled in-memory / MQTT)."""
        return True

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Get device by ID."""
        devices = db_service.get_all_devices()
        for d in devices:
            if d.get("device_id") == device_id or d.get("username") == device_id:
                return d
        return None

    def get_all_devices(self) -> List[Dict[str, Any]]:
        """Get all registered devices from AWS RDS PostgreSQL."""
        return db_service.get_all_devices()

    def get_active_devices(self) -> List[Dict[str, Any]]:
        """Get all registered devices."""
        return db_service.get_all_devices()

    def delete_device(self, device_id: Optional[str] = None, username: Optional[str] = None) -> bool:
        """Delete a single device record by device_id or username."""
        return db_service.delete_device(device_id=device_id, username=username)

    def delete_all_devices(self) -> bool:
        """Clear all registered devices from the aiml table."""
        return db_service.delete_all_devices()

    # ----------------------------------------------------------------------
    # Command History & Telemetry compatibility stubs
    # (Sprint 13 only uses the 'aiml' table; stubs prevent errors)
    # ----------------------------------------------------------------------
    def record_command_received(self, *args, **kwargs) -> Optional[int]:
        return None

    def update_command_status(self, *args, **kwargs) -> bool:
        return True

    def update_command_by_correlation(self, *args, **kwargs) -> bool:
        return True

    def get_command_history(self, *args, **kwargs) -> List[Dict[str, Any]]:
        return []

    def log_startup(self, version: str = "1.0", *args, **kwargs):
        pass

    def log_shutdown(self, *args, **kwargs):
        pass

    def log_system_event(self, *args, **kwargs):
        pass

    def _log_system_event(self, *args, **kwargs):
        pass

    def log_device_register(self, device_id: str, username: str, device_type: str, *args, **kwargs):
        pass


_store_instance: Optional[PostgresStore] = None


def get_postgres_store() -> PostgresStore:
    """Get the singleton PostgresStore instance."""
    global _store_instance
    if _store_instance is None:
        _store_instance = PostgresStore()
        _store_instance.initialize()
    return _store_instance


def init_postgres() -> bool:
    """Initialize PostgreSQL integration."""
    store = get_postgres_store()
    return store.initialize()


def close_postgres():
    """Close PostgreSQL integration."""
    global _store_instance
    if _store_instance:
        _store_instance.close()
        _store_instance = None