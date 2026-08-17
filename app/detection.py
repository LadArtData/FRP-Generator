"""Detection and payload build job handlers."""

from __future__ import annotations

import base64
import logging

from . import crypto, db
from .detection.engine import run_detection

log = logging.getLogger("warden.detection")


def _resolve_run_ids(job: dict) -> tuple[int, int]:
    tenant_id = int(job["tenant_id"])
    conflict_run_id = job.get("conflict_run_id") or db.latest_run_id(tenant_id, "CONFLICT")
    model_run_id = job.get("model_run_id") or db.latest_run_id(tenant_id, "MODEL")
    if not conflict_run_id or not model_run_id:
        raise RuntimeError(
            "DETECT requires completed CONFLICT and MODEL runs — enqueue ingest jobs first"
        )
    return int(conflict_run_id), int(model_run_id)


def process_detect(job: dict) -> None:
    tenant_id = int(job["tenant_id"])
    conflict_run_id, model_run_id = _resolve_run_ids(job)
    log.info("DETECT tenant=%s conflict_run=%s model_run=%s", tenant_id, conflict_run_id, model_run_id)

    tenant = db.get_tenant(tenant_id)
    rules = db.load_active_rules(tenant_id)
    if not rules:
        raise RuntimeError(f"no active rules for tenant {tenant_id} — load ruleset first")

    conflicts = db.load_staging_conflict(tenant_id, conflict_run_id)
    model = db.load_staging_model(tenant_id, model_run_id)
    if not conflicts:
        raise RuntimeError(f"no conflict staging for run {conflict_run_id}")
    if not model:
        raise RuntimeError(f"no model staging for run {model_run_id}")

    prov = db.build_prov(conflict_run_id, model_run_id)
    ledger = db.load_ledger(tenant_id)
    exceptions = db.load_unit_exceptions(tenant_id)

    payload = run_detection(
        tenant={"name": tenant["name"], "env": tenant["env"]},
        prov=prov,
        rules=rules,
        conflict_rows=conflicts,
        model_rows=model,
        ledger=ledger,
        exceptions=exceptions,
    )
    payload["prov"] = prov
    job["payload"] = payload
    job["prov"] = prov
    job["conflict_run_id"] = conflict_run_id
    job["model_run_id"] = model_run_id

    # Persist for the separate BUILD_PAYLOAD job (worker processes one job per poll).
    db.save_interim_payload(tenant_id, payload, prov)
    log.info(
        "detected %d people, %d real conflicts for tenant %s",
        payload["kpi"]["flagged"],
        payload["kpi"]["real"],
        tenant_id,
    )


def process_build_payload(job: dict) -> None:
    tenant_id = int(job["tenant_id"])
    passphrase = job.get("access_key")
    if not passphrase:
        raise RuntimeError("BUILD_PAYLOAD requires access_key in job payload")

    payload, prov = db.load_interim_payload(tenant_id)

    blob = crypto.encrypt_document({"data": payload, "prov": prov}, passphrase)
    crypto_blob = {
        "verifier": blob["verifier"],
        "salt_bytes": base64.b64decode(blob["salt"]),
        "iv_bytes": base64.b64decode(blob["iv"]),
        "ct_bytes": base64.b64decode(blob["ct"]),
        "key_salt_bytes": base64.b64decode(blob["salt"]),
    }
    db.save_tenant_payload(tenant_id, payload, prov, crypto_blob)
    log.info("tenant %s payload stored (%d people)", tenant_id, payload.get("kpi", {}).get("flagged", 0))
