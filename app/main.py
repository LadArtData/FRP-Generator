"""WARDEN worker — main loop (SCOUT pattern)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import db, ords
from .detection import process_build_payload, process_detect
from .ingest import process_ingest_conflict, process_ingest_model

LOG = logging.getLogger("warden.main")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-5s %(name)s — %(message)s",
)

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SEC", "10"))
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080"))

HANDLERS = {
    "INGEST_CONFLICT": process_ingest_conflict,
    "INGEST_MODEL": process_ingest_model,
    "DETECT": process_detect,
    "BUILD_PAYLOAD": process_build_payload,
}


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = b'{"ok":true}'
            if db.configured():
                body = b'{"ok":true,"database":"configured"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args, **kwargs):
        pass


def _start_health_server():
    srv = HTTPServer(("0.0.0.0", HEALTH_PORT), _Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    LOG.info("health server on :%d/health", HEALTH_PORT)


def _process(job: dict) -> None:
    job_id = job["job_id"]
    job_type = (job.get("job_type") or "").upper()
    payload = job.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    merged = {**job, **(payload or {})}

    handler = HANDLERS.get(job_type)
    if handler is None:
        ords.fail_job(job_id, f"unknown job_type {job_type}")
        return

    try:
        handler(merged)
        ords.complete_job(job_id)
    except Exception as exc:
        LOG.exception("job %s failed", job_id)
        ords.fail_job(job_id, exc)


def main():
    _start_health_server()
    if not ords.configured():
        LOG.error("ORDS not configured — set ORDS_BASE and WARDEN_API_KEY")
    if db.configured():
        try:
            db.init_pool()
            LOG.info("database pool initialized")
        except Exception:
            LOG.exception("database pool failed — ingest/detect jobs will fail")
    else:
        LOG.warning("Oracle not configured — DB-backed jobs unavailable")

    while True:
        try:
            for job in ords.claim_pending_jobs():
                _process(job)
        except Exception:
            LOG.exception("poll loop error")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
