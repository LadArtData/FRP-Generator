"""Ingestion job handlers — parse Fusion exports and bulk-load staging tables."""

from __future__ import annotations

import logging
from datetime import date

from . import db, ociclients
from .config import cfg
from .ingest.conflict import parse_conflict_csv
from .ingest.model import parse_model_csv

log = logging.getLogger("warden.ingest")


def _fetch_object(object_key: str) -> bytes:
    return ociclients.get_object(cfg.bucket_name, object_key)


def _resolve_run_id(job: dict, run_type: str) -> int:
    if job.get("run_id"):
        return int(job["run_id"])
    tenant_id = int(job["tenant_id"])
    run_key = job.get("run_key") or f"{run_type.lower()}_{job.get('object_key', 'import')}"
    as_of = job.get("as_of_date")
    as_of_date = date.fromisoformat(as_of) if as_of else None
    return db.ensure_run(
        tenant_id,
        run_key,
        run_type,
        object_key=job.get("object_key"),
        source_label=job.get("source_label"),
        as_of_date=as_of_date,
    )


def process_ingest_conflict(job: dict) -> None:
    tenant_id = int(job["tenant_id"])
    run_id = _resolve_run_id(job, "CONFLICT")
    object_key = job["object_key"]
    log.info("INGEST_CONFLICT tenant=%s run=%s key=%s", tenant_id, run_id, object_key)

    rows = parse_conflict_csv(_fetch_object(object_key))
    db.clear_staging_conflict(tenant_id, run_id)
    n = db.bulk_insert_conflict(tenant_id, run_id, rows)
    db.complete_run(run_id, {"rows": n, "object_key": object_key})
    job["run_id"] = run_id
    log.info("staged %d conflict rows for run %s", n, run_id)


def process_ingest_model(job: dict) -> None:
    tenant_id = int(job["tenant_id"])
    run_id = _resolve_run_id(job, "MODEL")
    object_key = job["object_key"]
    log.info("INGEST_MODEL tenant=%s run=%s key=%s", tenant_id, run_id, object_key)

    rows = parse_model_csv(_fetch_object(object_key))
    stats = db.model_stats_from_rows(rows)
    db.clear_staging_model(tenant_id, run_id)
    n = db.bulk_insert_model(tenant_id, run_id, rows)
    stats["rows"] = n
    stats["object_key"] = object_key
    db.complete_run(run_id, stats)
    job["run_id"] = run_id
    log.info("staged %d model rows for run %s", n, run_id)
