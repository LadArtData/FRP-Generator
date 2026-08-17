"""Oracle access for worker bulk staging, rules, runs, and payload persistence."""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any

import oracledb

from .config import cfg
from .detection.models import ConflictRow, ModelRow
from .rules import rule_from_row

log = logging.getLogger("warden.db")

BATCH = 5000
_pool: oracledb.ConnectionPool | None = None
_lock = threading.Lock()


class DbConfigError(RuntimeError):
    pass


def configured() -> bool:
    return bool(cfg.oracle_dsn and cfg.oracle_password)


def _init_session(connection, _requested_tag):
    cur = connection.cursor()
    try:
        cur.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {cfg.app_schema}")
    finally:
        cur.close()


def init_pool() -> oracledb.ConnectionPool:
    global _pool
    if not configured():
        raise DbConfigError("Oracle DSN and password required for worker DB access")
    with _lock:
        if _pool is not None:
            return _pool
        kwargs: dict[str, Any] = {
            "user": cfg.oracle_user,
            "password": cfg.oracle_password,
            "dsn": cfg.oracle_dsn,
            "min": cfg.pool_min,
            "max": cfg.pool_max,
            "increment": 1,
            "session_callback": _init_session,
            "tcp_connect_timeout": cfg.connect_timeout,
        }
        if cfg.tns_admin:
            kwargs["config_dir"] = cfg.tns_admin
            kwargs["wallet_location"] = cfg.tns_admin
            if cfg.wallet_password:
                kwargs["wallet_password"] = cfg.wallet_password
        _pool = oracledb.create_pool(**kwargs)
        log.info("oracle pool ready dsn=%s schema=%s", cfg.oracle_dsn, cfg.app_schema)
        return _pool


def close_pool() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close(force=True)
            _pool = None


@contextmanager
def connection():
    conn = init_pool().acquire()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        init_pool().release(conn)


# ---------------------------------------------------------------------------
# Tenants & runs
# ---------------------------------------------------------------------------

def get_tenant(tenant_id: int) -> dict[str, Any]:
    with connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT tenant_id, name, env, bucket_prefix
              FROM warden_tenants
             WHERE tenant_id = :tid AND status = 'active'
            """,
            {"tid": tenant_id},
        )
        row = cur.fetchone()
        if not row:
            raise LookupError(f"tenant {tenant_id} not found")
        return {"tenant_id": row[0], "name": row[1], "env": row[2], "bucket_prefix": row[3]}


def ensure_run(
    tenant_id: int,
    run_key: str,
    run_type: str,
    *,
    object_key: str | None = None,
    source_label: str | None = None,
    as_of_date: date | None = None,
) -> int:
    with connection() as conn:
        cur = conn.cursor()
        out = cur.var(oracledb.NUMBER)
        cur.execute(
            """
            MERGE INTO warden_runs t
            USING (SELECT :tid AS tenant_id, :rkey AS run_key FROM dual) s
               ON (t.tenant_id = s.tenant_id AND t.run_key = s.run_key)
            WHEN MATCHED THEN
              UPDATE SET object_key = NVL(:obj, t.object_key),
                         source_label = NVL(:src, t.source_label),
                         as_of_date = NVL(:asof, t.as_of_date),
                         status = 'pending'
            WHEN NOT MATCHED THEN
              INSERT (tenant_id, run_key, run_type, object_key, source_label, as_of_date, status)
              VALUES (:tid, :rkey, :rtype, :obj, :src, :asof, 'pending')
            """,
            {
                "tid": tenant_id,
                "rkey": run_key,
                "rtype": run_type,
                "obj": object_key,
                "src": source_label,
                "asof": as_of_date,
            },
        )
        cur.execute(
            """
            SELECT run_id FROM warden_runs
             WHERE tenant_id = :tid AND run_key = :rkey
            """,
            {"tid": tenant_id, "rkey": run_key},
        )
        run_id = cur.fetchone()[0]
        return int(run_id)


def complete_run(run_id: int, stats: dict[str, Any] | None = None) -> None:
    with connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE warden_runs
               SET status = 'done',
                   stats_json = :stats,
                   completed_at = SYSTIMESTAMP
             WHERE run_id = :rid
            """,
            {"rid": run_id, "stats": json.dumps(stats or {})},
        )


def get_run(run_id: int) -> dict[str, Any]:
    with connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT run_id, tenant_id, run_key, run_type, as_of_date,
                   source_label, object_key, stats_json, status
              FROM warden_runs WHERE run_id = :rid
            """,
            {"rid": run_id},
        )
        row = cur.fetchone()
        if not row:
            raise LookupError(f"run {run_id} not found")
        return {
            "run_id": row[0],
            "tenant_id": row[1],
            "run_key": row[2],
            "run_type": row[3],
            "as_of_date": row[4],
            "source_label": row[5],
            "object_key": row[6],
            "stats_json": row[7],
            "status": row[8],
        }


def latest_run_id(tenant_id: int, run_type: str) -> int | None:
    with connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT run_id FROM warden_runs
             WHERE tenant_id = :tid AND run_type = :rtype AND status = 'done'
             ORDER BY completed_at DESC NULLS LAST, run_id DESC
             FETCH FIRST 1 ROW ONLY
            """,
            {"tid": tenant_id, "rtype": run_type},
        )
        row = cur.fetchone()
        return int(row[0]) if row else None


def build_prov(conflict_run_id: int, model_run_id: int) -> dict[str, Any]:
    conflict = get_run(conflict_run_id)
    model = get_run(model_run_id)
    c_stats = json.loads(conflict.get("stats_json") or "{}")
    m_stats = json.loads(model.get("stats_json") or "{}")
    c_date = conflict.get("as_of_date")
    m_date = model.get("as_of_date")
    drift = 0
    if c_date and m_date:
        drift = abs((m_date - c_date).days)
    return {
        "conflict": {
            "pretty": _format_date(c_date),
            "source": conflict.get("source_label") or "User-level SOD analysis",
        },
        "model": {
            "pretty": _format_date(m_date),
            "source": model.get("source_label") or "User and Role Access Audit Report",
            "scope": m_stats.get("scope", "HCM application stripe only"),
            "roles": m_stats.get("roles", 0),
            "grants": m_stats.get("grants", 0),
        },
        "drift_days": drift,
    }


def _format_date(d: date | datetime | None) -> str:
    if d is None:
        return "—"
    if isinstance(d, datetime):
        d = d.date()
    return d.strftime("%B %d, %Y").replace(" 0", " ")


# ---------------------------------------------------------------------------
# Staging — bulk insert
# ---------------------------------------------------------------------------

def clear_staging_conflict(tenant_id: int, run_id: int) -> None:
    with connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM warden_staging_conflict WHERE tenant_id = :tid AND run_id = :rid",
            {"tid": tenant_id, "rid": run_id},
        )


def clear_staging_model(tenant_id: int, run_id: int) -> None:
    with connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM warden_staging_model WHERE tenant_id = :tid AND run_id = :rid",
            {"tid": tenant_id, "rid": run_id},
        )


def bulk_insert_conflict(tenant_id: int, run_id: int, rows: list[ConflictRow]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO warden_staging_conflict
          (tenant_id, run_id, person_ref, rule_id, disposition, unit, raw_json)
        VALUES (:1, :2, :3, :4, :5, :6, :7)
    """
    data = [
        (
            tenant_id,
            run_id,
            r.person_ref,
            r.rule_id,
            r.disposition,
            r.unit,
            json.dumps({"person_ref": r.person_ref, "rule_id": r.rule_id, "unit": r.unit}),
        )
        for r in rows
    ]
    with connection() as conn:
        cur = conn.cursor()
        for i in range(0, len(data), BATCH):
            cur.executemany(sql, data[i : i + BATCH])
    return len(rows)


def bulk_insert_model(tenant_id: int, run_id: int, rows: list[ModelRow]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO warden_staging_model
          (tenant_id, run_id, entity_type, entity_key, parent_key, raw_json)
        VALUES (:1, :2, :3, :4, :5, :6)
    """
    data = [
        (
            tenant_id,
            run_id,
            r.entity_type.upper(),
            r.entity_key,
            r.parent_key,
            json.dumps({"entity_type": r.entity_type, "entity_key": r.entity_key,
                        "parent_key": r.parent_key, "attrs": r.attrs}),
        )
        for r in rows
    ]
    with connection() as conn:
        cur = conn.cursor()
        for i in range(0, len(data), BATCH):
            cur.executemany(sql, data[i : i + BATCH])
    return len(rows)


def load_staging_conflict(tenant_id: int, run_id: int) -> list[ConflictRow]:
    with connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT person_ref, rule_id, unit, disposition
              FROM warden_staging_conflict
             WHERE tenant_id = :tid AND run_id = :rid
            """,
            {"tid": tenant_id, "rid": run_id},
        )
        return [
            ConflictRow(person_ref=r[0], rule_id=r[1], unit=r[2] or "Unattributed", disposition=r[3] or "")
            for r in cur.fetchall()
        ]


def load_staging_model(tenant_id: int, run_id: int) -> list[ModelRow]:
    with connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT entity_type, entity_key, parent_key, raw_json
              FROM warden_staging_model
             WHERE tenant_id = :tid AND run_id = :rid
            """,
            {"tid": tenant_id, "rid": run_id},
        )
        rows: list[ModelRow] = []
        for et, ek, pk, raw in cur.fetchall():
            attrs = {}
            if raw:
                try:
                    parsed = json.loads(raw)
                    attrs = parsed.get("attrs") or {}
                except json.JSONDecodeError:
                    pass
            rows.append(ModelRow(entity_type=et, entity_key=ek, parent_key=pk or "", attrs=attrs))
        return rows


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def load_active_rules(tenant_id: int) -> list:
    with connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT rule_id, name, tier, config_json
              FROM warden_rules
             WHERE tenant_id = :tid AND status = 'active'
             ORDER BY rule_id
            """,
            {"tid": tenant_id},
        )
        rules = [rule_from_row(r) for r in cur.fetchall()]
        log.info("loaded %d active rules for tenant %s", len(rules), tenant_id)
        return rules


def upsert_rule(tenant_id: int, doc: dict[str, Any]) -> None:
    from .rules import rule_to_config_json

    with connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            MERGE INTO warden_rules t
            USING (SELECT :tid AS tenant_id, :rid AS rule_id FROM dual) s
               ON (t.tenant_id = s.tenant_id AND t.rule_id = s.rule_id)
            WHEN MATCHED THEN
              UPDATE SET name = :name, cycle = :cycle, module = :module,
                         tier = :tier, status = :status,
                         cross_module = :cross, config_json = :cfg,
                         updated_at = SYSTIMESTAMP
            WHEN NOT MATCHED THEN
              INSERT (tenant_id, rule_id, name, cycle, module, tier, status,
                      cross_module, config_json)
              VALUES (:tid, :rid, :name, :cycle, :module, :tier, :status,
                      :cross, :cfg)
            """,
            {
                "tid": tenant_id,
                "rid": doc["rule_id"],
                "name": doc.get("name", doc["rule_id"]),
                "cycle": doc.get("cycle"),
                "module": doc.get("module"),
                "tier": doc.get("tier", "moderate"),
                "status": doc.get("status", "active"),
                "cross": "Y" if doc.get("cross_module") else "N",
                "cfg": rule_to_config_json(doc),
            },
        )


# ---------------------------------------------------------------------------
# Ledger & exceptions
# ---------------------------------------------------------------------------

def load_ledger(tenant_id: int) -> list[dict[str, Any]]:
    with connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT entry_key, scope, decision, target_ref, rule_id, rationale,
                   decided_by, decided_at, expires_at, evidence_label, status
              FROM warden_ledger
             WHERE tenant_id = :tid AND status <> 'superseded'
             ORDER BY created_at DESC
            """,
            {"tid": tenant_id},
        )
        out = []
        for row in cur.fetchall():
            out.append({
                "key": row[0],
                "scope": row[1],
                "decision": row[2],
                "target": row[3],
                "rule": row[4],
                "rationale": row[5],
                "owner": row[6],
                "decided": row[7].strftime("%Y-%m-%d") if row[7] else "",
                "expires": row[8].strftime("%Y-%m-%d") if row[8] else "",
                "evidence": row[9],
                "status": row[10],
            })
        return out


def load_unit_exceptions(tenant_id: int) -> list[dict[str, str]]:
    with connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT rule_id, unit FROM warden_unit_exceptions
                 WHERE tenant_id = :tid
                """,
                {"tid": tenant_id},
            )
            return [{"rule": r[0], "unit": r[1]} for r in cur.fetchall()]
        except oracledb.DatabaseError:
            return []


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

def save_interim_payload(tenant_id: int, payload: dict, prov: dict) -> None:
    """Store detection output before BUILD_PAYLOAD applies encryption."""
    with connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE warden_tenants
               SET payload_json = :payload,
                   prov_json = :prov,
                   updated_at = SYSTIMESTAMP
             WHERE tenant_id = :tid
            """,
            {"payload": json.dumps(payload), "prov": json.dumps(prov), "tid": tenant_id},
        )


def load_interim_payload(tenant_id: int) -> tuple[dict, dict]:
    with connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT payload_json, prov_json FROM warden_tenants WHERE tenant_id = :tid",
            {"tid": tenant_id},
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            raise LookupError(f"no interim payload for tenant {tenant_id}")
        return json.loads(row[0]), json.loads(row[1] or "{}")


def save_tenant_payload(tenant_id: int, payload: dict, prov: dict, crypto_blob: dict) -> None:
    with connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE warden_tenants
               SET payload_json = :payload,
                   prov_json = :prov,
                   payload_salt = :salt,
                   payload_iv = :iv,
                   payload_ct = :ct,
                   key_verifier = :verifier,
                   key_salt = :key_salt,
                   updated_at = SYSTIMESTAMP
             WHERE tenant_id = :tid
            """,
            {
                "payload": json.dumps(payload),
                "prov": json.dumps(prov),
                "salt": crypto_blob.get("salt_bytes"),
                "iv": crypto_blob.get("iv_bytes"),
                "ct": crypto_blob.get("ct_bytes"),
                "verifier": crypto_blob.get("verifier"),
                "key_salt": crypto_blob.get("key_salt_bytes"),
                "tid": tenant_id,
            },
        )


def model_stats_from_rows(rows: list[ModelRow]) -> dict[str, int]:
    roles = {r.entity_key for r in rows if r.entity_type.upper() == "ROLE"}
    grants = sum(1 for r in rows if r.entity_type.upper() == "GRANT")
    return {"roles": len(roles), "grants": grants, "scope": "HCM application stripe only"}
