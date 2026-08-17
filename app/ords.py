"""ORDS client for WARDEN worker."""

import json
import logging
import os

import requests

LOG = logging.getLogger("warden.ords")

BASE = os.getenv("ORDS_BASE", "").rstrip("/")
API_KEY = os.getenv("WARDEN_API_KEY", "")


def configured() -> bool:
    return bool(BASE and API_KEY)


def _headers() -> dict:
    return {"api-key": API_KEY, "Content-Type": "application/json"}


def _url(path: str) -> str:
    return f"{BASE}{path}?api_key={API_KEY}"


def claim_pending_jobs() -> list[dict]:
    if not configured():
        return []
    res = requests.get(_url("/jobs/pending"), headers=_headers(), timeout=60)
    res.raise_for_status()
    body = res.json()
    return body.get("items") or []


def complete_job(job_id: int) -> None:
    requests.post(_url(f"/jobs/{job_id}/complete"), headers=_headers(), timeout=60).raise_for_status()


def fail_job(job_id: int, error: Exception | str) -> None:
    msg = str(error)
    requests.post(
        _url(f"/jobs/{job_id}/fail"),
        headers=_headers(),
        data=json.dumps({"error": msg}),
        timeout=60,
    ).raise_for_status()
