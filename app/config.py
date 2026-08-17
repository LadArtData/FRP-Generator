"""WARDEN worker configuration."""

from __future__ import annotations

import os


class Config:
    ords_base: str = os.getenv("ORDS_BASE", "").rstrip("/")
    api_key: str = os.getenv("WARDEN_API_KEY", "")

    oracle_user: str = os.getenv("ORACLE_USER", "ADMIN")
    oracle_password: str = os.getenv("ORACLE_PASSWORD", "")
    oracle_dsn: str = os.getenv("ORACLE_DSN", "")
    wallet_password: str = os.getenv("ORACLE_WALLET_PASSWORD", "")
    tns_admin: str = os.getenv("TNS_ADMIN", "/wallet")
    app_schema: str = os.getenv("WARDEN_APP_SCHEMA", "ITERIA_AI")

    oci_region: str = os.getenv("OCI_REGION", "")
    bucket_namespace: str = os.getenv("OCI_OBJECT_NAMESPACE", "")
    bucket_name: str = os.getenv("WARDEN_BUCKET_NAME", "")

    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    poll_interval_sec: int = int(os.getenv("POLL_INTERVAL_SEC", "10"))

    pool_min: int = int(os.getenv("DB_POOL_MIN", "1"))
    pool_max: int = int(os.getenv("DB_POOL_MAX", "4"))
    connect_timeout: int = int(os.getenv("DB_CONNECT_TIMEOUT", "30"))


cfg = Config()
