#!/usr/bin/env python3
"""Enqueue the standard WARDEN ingest → detect → build pipeline for a tenant.

Usage:
  python tools/enqueue_pipeline.py --tenant-id 1 \\
    --conflict-key tenants/acme/conflict.csv \\
    --model-key tenants/acme/model.csv \\
    --access-key "<tenant passphrase>"

Requires ORDS_BASE and WARDEN_API_KEY in the environment (or .env).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def enqueue(base: str, api_key: str, job_type: str, tenant_id: int, payload: dict) -> int:
    url = f"{base.rstrip('/')}/jobs?api_key={api_key}"
    body = {"job_type": job_type, "tenant_id": tenant_id, "payload": payload}
    res = requests.post(url, headers={"api-key": api_key, "Content-Type": "application/json"},
                        data=json.dumps(body), timeout=60)
    res.raise_for_status()
    return res.json().get("job_id")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Enqueue WARDEN pipeline jobs")
    p.add_argument("--tenant-id", type=int, required=True)
    p.add_argument("--conflict-key", required=True, help="Object Storage key for conflict CSV")
    p.add_argument("--model-key", required=True, help="Object Storage key for model CSV")
    p.add_argument("--access-key", required=True, help="Tenant passphrase for BUILD_PAYLOAD")
    p.add_argument("--conflict-label", default="User-level SOD analysis")
    p.add_argument("--model-label", default="User and Role Access Audit Report")
    p.add_argument("--as-of-conflict", help="YYYY-MM-DD")
    p.add_argument("--as-of-model", help="YYYY-MM-DD")
    args = p.parse_args(argv)

    base = os.environ.get("ORDS_BASE", "")
    api_key = os.environ.get("WARDEN_API_KEY", "")
    if not base or not api_key:
        print("Set ORDS_BASE and WARDEN_API_KEY", file=sys.stderr)
        return 1

    conflict_payload = {
        "object_key": args.conflict_key,
        "source_label": args.conflict_label,
    }
    model_payload = {
        "object_key": args.model_key,
        "source_label": args.model_label,
    }
    if args.as_of_conflict:
        conflict_payload["as_of_date"] = args.as_of_conflict
    if args.as_of_model:
        model_payload["as_of_date"] = args.as_of_model

    j1 = enqueue(base, api_key, "INGEST_CONFLICT", args.tenant_id, conflict_payload)
    j2 = enqueue(base, api_key, "INGEST_MODEL", args.tenant_id, model_payload)
    j3 = enqueue(base, api_key, "DETECT", args.tenant_id, {})
    j4 = enqueue(base, api_key, "BUILD_PAYLOAD", args.tenant_id, {"access_key": args.access_key})
    print(f"Enqueued jobs: INGEST_CONFLICT={j1}, INGEST_MODEL={j2}, DETECT={j3}, BUILD_PAYLOAD={j4}")
    print("Worker will process in order as each completes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
