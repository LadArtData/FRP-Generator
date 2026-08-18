"""Oracle Autonomous Database access.

A single pooled connection source with an explicit transaction boundary. Every
write path uses `transaction()`, which commits on success and rolls back on any
exception, so a failure mid-write cannot leave a half-written bid behind.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager

import oracledb

from .config import cfg
from .errors import HaraldError

log = logging.getLogger("harald.db")

_pool: oracledb.ConnectionPool | None = None
_lock = threading.Lock()


def _init_session(connection, requested_tag):  # noqa: ARG001 (oracledb callback signature)
    """Run on each new pooled session. The app authenticates as ADMIN, the one ADB
    login, but every product table lives in the ITERIA_AI schema. Setting
    CURRENT_SCHEMA makes every unqualified query in the app resolve to ITERIA_AI,
    which is equivalent to prefixing each one with iteria_ai. but cannot be missed."""
    cursor = connection.cursor()
    try:
        cursor.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {cfg.app_schema}")
    finally:
        cursor.close()


def init_pool() -> oracledb.ConnectionPool:
    """Create the pool. Safe to call repeatedly; only the first call builds it."""
    global _pool
    with _lock:
        if _pool is not None:
            return _pool
        kwargs: dict = {
            "user": cfg.oracle_user,
            "password": cfg.oracle_password,
            "dsn": cfg.oracle_dsn,
            "min": cfg.pool_min,
            "max": cfg.pool_max,
            "increment": 1,
            "ping_interval": 60,
            "getmode": oracledb.POOL_GETMODE_WAIT,
            "session_callback": _init_session,
            # An unreachable ADB otherwise takes three and a half minutes to
            # report, which during a deploy looks like a hung container rather
            # than a blocked route.
            "tcp_connect_timeout": cfg.connect_timeout,
        }
        if cfg.retry_count:
            kwargs["retry_count"] = cfg.retry_count
            kwargs["retry_delay"] = 1
        if cfg.tns_admin:
            kwargs["config_dir"] = cfg.tns_admin
            kwargs["wallet_location"] = cfg.tns_admin
            if cfg.wallet_password:
                kwargs["wallet_password"] = cfg.wallet_password
        _pool = oracledb.create_pool(**kwargs)
        log.info("oracle pool created dsn=%s schema=%s min=%s max=%s",
                 cfg.oracle_dsn, cfg.app_schema, cfg.pool_min, cfg.pool_max)
        return _pool


def close_pool() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close(force=True)
            _pool = None
            log.info("oracle pool closed")


def _pool_or_init() -> oracledb.ConnectionPool:
    return _pool if _pool is not None else init_pool()


@contextmanager
def connection():
    """Read-only or self-managed connection. Does not commit."""
    conn = _pool_or_init().acquire()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction():
    """Write transaction. Commits on clean exit, rolls back on any exception."""
    conn = _pool_or_init().acquire()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:  # rollback failure must not mask the original error
            log.exception("rollback failed")
        raise
    finally:
        conn.close()


@contextmanager
def cursor():
    """Read cursor convenience wrapper."""
    with connection() as conn:
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()


def read_lob(value):
    """Oracle LOBs arrive as locators; normalise to str/bytes."""
    if value is None:
        return None
    return value.read() if hasattr(value, "read") else value


def clob(value) -> str:
    return read_lob(value) or ""


def healthcheck() -> bool:
    try:
        with cursor() as cur:
            cur.execute("SELECT 1 FROM dual")
            return cur.fetchone()[0] == 1
    except Exception:
        log.exception("database healthcheck failed")
        return False
