"""
Device Database Service (AWS RDS PostgreSQL Integration)
========================================================
Provides persistent storage for registered mobile/BCI devices using AWS RDS PostgreSQL.
Guarantees device persistence across transmitter server restarts, replacing Supabase.

Schema:
    CREATE TABLE IF NOT EXISTS aiml (
        device_id   TEXT PRIMARY KEY,
        username    TEXT UNIQUE NOT NULL,
        device_type TEXT
    );
"""
from __future__ import annotations

import os
import ssl
import time
import logging
from typing import List, Dict, Optional, Any

from pathlib import Path
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

logger = logging.getLogger(__name__)

SYNC_API_ENABLED = os.environ.get("SYNC_API_ENABLED", "true").lower() in ("true", "1", "yes")
try:
    from . import sync_api_client as sync_client
except ImportError:
    try:
        from mobile_dashboard.mobile_plugin.services import sync_api_client as sync_client
    except ImportError:
        try:
            import sync_api_client as sync_client
        except ImportError:
            sync_client = None


# Connection parameters (Strictly from .env)
DB_HOST = os.environ.get("DB_HOST", "")
DB_PORT = int(os.environ.get("DB_PORT", 5432))
DB_FALLBACK_HOST = os.environ.get("DB_FALLBACK_HOST", "")
DB_FALLBACK_PORT = int(os.environ.get("DB_FALLBACK_PORT", 15432)) if os.environ.get("DB_FALLBACK_PORT") else None
DB_NAME = os.environ.get("DB_NAME", "")
DB_USER = os.environ.get("DB_USER", "")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_SSLMODE = os.environ.get("DB_SSLMODE", "require").lower()

ACTIVE_HOST = DB_HOST
ACTIVE_PORT = DB_PORT

_schema_verified = False

_last_db_failure = 0.0
_DB_FAILURE_COOLDOWN = 30.0

def _get_connection():
    """Establish a database connection to PostgreSQL (pg8000 or psycopg2).
    Attempts primary endpoint first (AWS RDS / VPN), and falls back to local SSH tunnel (127.0.0.1:15432).
    Includes a 30s circuit breaker to avoid blocking requests when PostgreSQL is offline.
    """
    global _last_db_failure, ACTIVE_HOST, ACTIVE_PORT
    if SYNC_API_ENABLED:
        # Direct RDS ports (5432 / 15432) are permanently closed by DevOps in favor of Sync API.
        # Bypassing raw socket attempts prevents 5-second blocking timeouts and circuit breaker warnings.
        return None
    now = time.time()
    if now - _last_db_failure < _DB_FAILURE_COOLDOWN:
        return None

    driver_found = False
    last_error = None

    candidates = []
    if DB_HOST:
        candidates.append((DB_HOST, DB_PORT))
    if DB_FALLBACK_HOST and DB_FALLBACK_PORT and (DB_FALLBACK_HOST, DB_FALLBACK_PORT) not in candidates:
        candidates.append((DB_FALLBACK_HOST, DB_FALLBACK_PORT))

    for host_cand, port_cand in candidates:
        # 1. Try pg8000 (Pure Python driver)
        try:
            import pg8000.dbapi
            driver_found = True
            ssl_ctx = None
            if DB_SSLMODE in ("require", "prefer", "verify-ca", "verify-full"):
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE

            conn = pg8000.dbapi.connect(
                host=host_cand,
                port=port_cand,
                database=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD,
                ssl_context=ssl_ctx,
                timeout=5.0,
            )
            _last_db_failure = 0.0
            ACTIVE_HOST, ACTIVE_PORT = host_cand, port_cand
            return conn
        except ImportError:
            pass
        except Exception as e:
            last_error = e

        # 2. Try psycopg2
        try:
            import psycopg2
            driver_found = True
            conn = psycopg2.connect(
                host=host_cand,
                port=port_cand,
                dbname=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD,
                sslmode=DB_SSLMODE if DB_SSLMODE != "disable" else "disable",
                connect_timeout=5,
            )
            _last_db_failure = 0.0
            ACTIVE_HOST, ACTIVE_PORT = host_cand, port_cand
            return conn
        except ImportError:
            pass
        except Exception as e:
            last_error = e

    _last_db_failure = time.time()
    if not driver_found:
        logger.error("[POSTGRES] No usable PostgreSQL driver found (install pg8000 or psycopg2-binary).")
    elif last_error:
        candidate_strs = [f"{h}:{p}" for h, p in candidates]
        logger.warning(f"[POSTGRES] Cannot reach endpoints {candidate_strs} (circuit breaker active for 30s): {last_error}")
    return None


def ensure_schema() -> bool:
    """Ensure the aiml table exists with the specified schema."""
    global _schema_verified
    if _schema_verified:
        return True
    if SYNC_API_ENABLED and sync_client:
        _schema_verified = True
        return True

    conn = _get_connection()
    if not conn:
        return False

    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS aiml (
                device_id   TEXT PRIMARY KEY,
                username    TEXT UNIQUE NOT NULL,
                device_type TEXT
            );
            """
        )
        conn.commit()
        cur.close()
        conn.close()
        _schema_verified = True
        logger.info("[POSTGRES] Verified aiml table schema on AWS RDS PostgreSQL.")
        return True
    except Exception as e:
        logger.error(f"[POSTGRES] Error ensuring schema: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return False


def get_all_devices() -> List[Dict[str, str]]:
    """Fetch all registered devices from the aiml table."""
    if SYNC_API_ENABLED and sync_client:
        try:
            devs = sync_client.get_all_devices()
            if devs:
                return devs
        except Exception as e:
            logger.debug(f"[POSTGRES] Sync API device list fallback: {e}")
    ensure_schema()
    conn = _get_connection()
    if not conn:
        return []

    try:
        cur = conn.cursor()
        cur.execute("SELECT device_id, username, device_type FROM aiml;")
        rows = cur.fetchall()
        cur.close()
        conn.close()

        devices = []
        for r in rows:
            devices.append({
                "device_id": str(r[0]),
                "username": str(r[1]).lower(),
                "device_type": str(r[2]) if r[2] else "android",
            })
        logger.debug(f"[POSTGRES] Successfully fetched {len(devices)} device(s) from database.")
        return devices
    except Exception as e:
        logger.error(f"[POSTGRES] Error fetching devices: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return []


def upsert_device(device_id: str, username: str, device_type: str = "android") -> Optional[Dict[str, str]]:
    """
    Insert or update a device record in PostgreSQL using device_id as primary key
    and username as unique conflict target.
    
    If username changed for an existing device_id, or if a user re-registered on a 
    new/reinstalled device, it updates the record seamlessly without violating the
    unique username constraint.
    """
    if not device_id or not username:
        logger.warning("[POSTGRES] Invalid device_id or username for upsert.")
        return None

    clean_dev_id = str(device_id).strip()
    clean_uname = str(username).strip().lower()
    clean_type = (device_type or "android").strip().lower()

    if SYNC_API_ENABLED and sync_client:
        try:
            res = sync_client.register_device(clean_dev_id, clean_uname, clean_type)
            if res and (res.get("status") in ("registered", "success") or "device" in res):
                logger.info(f"[SYNC API] Upserted device '{clean_uname}' (ID={clean_dev_id[:8]}...) successfully.")
                return {
                    "device_id": clean_dev_id,
                    "username": clean_uname,
                    "device_type": clean_type,
                }
            elif res and res.get("status") == "conflict":
                # Handle reinstall / re-registration: old device had this username with a different UUID
                old_dev = sync_client.get_device_by_username(clean_uname)
                if old_dev and str(old_dev.get("device_id") or "").strip().lower() != clean_dev_id:
                    old_id = old_dev.get("device_id")
                    logger.info(f"[SYNC API] Resolving conflict for '{clean_uname}': replacing old device {old_id} with {clean_dev_id}")
                    sync_client.delete_device(old_id)
                    retry_res = sync_client.register_device(clean_dev_id, clean_uname, clean_type)
                    if retry_res and (retry_res.get("status") in ("registered", "success") or "device" in retry_res):
                        logger.info(f"[SYNC API] Upserted device '{clean_uname}' (ID={clean_dev_id[:8]}...) after replacing old UUID.")
                        return {
                            "device_id": clean_dev_id,
                            "username": clean_uname,
                            "device_type": clean_type,
                        }
                logger.warning(f"[SYNC API] Registration conflict for username '{clean_uname}': {res.get('error')}")
                return None
            else:
                logger.warning(f"[SYNC API] Device registration response: {res}")
                return {
                    "device_id": clean_dev_id,
                    "username": clean_uname,
                    "device_type": clean_type,
                }
        except Exception as e:
            logger.error(f"[SYNC API] Error upserting device: {e}")
            return None

    ensure_schema()
    conn = _get_connection()
    if not conn:
        return None

    try:
        cur = conn.cursor()
        # Step 1: Pre-clean any existing row with this username on a different device_id
        # (mirroring Sprint 13's delete_old_uuid logic to satisfy UNIQUE constraint on username)
        cur.execute(
            "DELETE FROM aiml WHERE username = %s AND device_id != %s;",
            (clean_uname, clean_dev_id),
        )

        # Step 2: Atomic Upsert by primary key
        cur.execute(
            """
            INSERT INTO aiml (device_id, username, device_type)
            VALUES (%s, %s, %s)
            ON CONFLICT (device_id) DO UPDATE SET
                username = EXCLUDED.username,
                device_type = EXCLUDED.device_type;
            """,
            (clean_dev_id, clean_uname, clean_type),
        )
        conn.commit()
        cur.close()
        conn.close()

        logger.info(f"[POSTGRES] Upserted device '{clean_uname}' (ID={clean_dev_id[:8]}...) successfully.")
        return {
            "device_id": clean_dev_id,
            "username": clean_uname,
            "device_type": clean_type,
        }
    except Exception as e:
        logger.error(f"[POSTGRES] Upsert device error: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return None


def delete_device(device_id: Optional[str] = None, username: Optional[str] = None) -> bool:
    """Delete a single device record from the aiml table by device_id or username."""
    if not device_id and not username:
        return False

    if SYNC_API_ENABLED and sync_client:
        try:
            target_id = str(device_id).strip() if device_id else None
            clean_user = str(username).strip().lower() if username else None

            # 1. If target_id not provided, try finding device_id by username
            if not target_id and clean_user:
                dev = sync_client.get_device_by_username(clean_user)
                if dev:
                    target_id = dev.get("device_id")

            # 2. If target_id still None, check if username itself was passed as identifier
            if not target_id and clean_user:
                dev = sync_client.get_device(clean_user)
                if dev:
                    target_id = dev.get("device_id")
                else:
                    target_id = clean_user

            if target_id:
                if sync_client.delete_device(target_id):
                    logger.info(f"[SYNC API] Deleted device id={target_id} (user={username}) from database.")
                    return True
                if clean_user and target_id != clean_user:
                    if sync_client.delete_device(clean_user):
                        logger.info(f"[SYNC API] Deleted device by username={clean_user} from database.")
                        return True
            return False
        except Exception as e:
            logger.error(f"[SYNC API] Error deleting device: {e}")
            return False

    ensure_schema()
    conn = _get_connection()
    if not conn:
        return False

    try:
        cur = conn.cursor()
        if device_id and username:
            cur.execute(
                "DELETE FROM aiml WHERE device_id = %s OR username = %s;",
                (str(device_id).strip(), str(username).strip().lower()),
            )
        elif device_id:
            cur.execute(
                "DELETE FROM aiml WHERE device_id = %s;",
                (str(device_id).strip(),),
            )
        else:
            cur.execute(
                "DELETE FROM aiml WHERE username = %s;",
                (str(username).strip().lower(),),
            )
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"[POSTGRES] Deleted device (id={device_id}, user={username}) from database.")
        return True
    except Exception as e:
        logger.error(f"[POSTGRES] Delete device error: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return False


def delete_all_devices() -> bool:
    """Clear all registered devices from the aiml table."""
    if SYNC_API_ENABLED and sync_client:
        try:
            devs = sync_client.get_all_devices()
            success = True
            for d in devs:
                dev_id = d.get("device_id")
                if dev_id:
                    if not sync_client.delete_device(dev_id):
                        success = False
            logger.info("[SYNC API] Cleared all devices from database via Sync API.")
            return success
        except Exception as e:
            logger.error(f"[SYNC API] Error clearing all devices: {e}")
            return False

    ensure_schema()
    conn = _get_connection()
    if not conn:
        return False

    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM aiml;")
        conn.commit()
        cur.close()
        conn.close()
        logger.info("[POSTGRES] Cleared all devices from database.")
        return True
    except Exception as e:
        logger.error(f"[POSTGRES] Delete all devices error: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return False
