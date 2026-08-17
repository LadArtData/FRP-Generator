"""Run TTUHSC RFP test against live HAROLD API.

Usage:
  python tools/test_rfp_live.py
  python tools/test_rfp_live.py --base http://137.23.54.1:8000
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

import httpx
from docx import Document

SAMPLE_RFP = Path(__file__).resolve().parent / "fixtures" / "ttuhsc_rfp739.txt"

DEFAULT_BASE = "http://137.23.54.1:8000"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--skip-generate", action="store_true",
                    help="Stop after parse/shred (faster smoke test)")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    if not SAMPLE_RFP.exists():
        print(f"Missing fixture: {SAMPLE_RFP}", file=sys.stderr)
        return 1

    rfp_text = SAMPLE_RFP.read_text(encoding="utf-8")
    # Live container may predate .txt extract support — upload as DOCX for reliability.
    doc = Document()
    for para in rfp_text.split("\n"):
        doc.add_paragraph(para)
    buf = io.BytesIO()
    doc.save(buf)
    rfp_bytes = buf.getvalue()
    upload_name = "RFP-739-SL3821039-TTUHSC-AI.docx"
    print(f"=== HAROLD live test -> {base} ===")
    print(f"fixture: {SAMPLE_RFP.name} -> {upload_name} ({len(rfp_bytes)} bytes)")
    print()

    with httpx.Client(timeout=300.0) as client:
        # Health
        health = client.get(f"{base}/api/health").json()
        print("health ok:", health.get("ok"))
        print("site_grounding:", health.get("site_grounding"))
        print("auto_humanize:", health.get("auto_humanize"))
        print("library docs:", (health.get("library") or {}).get("narrative_docs"))
        print()

        # Copilot smoke (no auth)
        copilot_q = (
            "For TTUHSC RFP 739-SL3821039 Enterprise AI Adoption: how should iteria "
            "respond about governance, HIPAA alignment, and staff training without "
            "saying we cannot meet the requirement?"
        )
        print("--- copilot ---")
        cop = client.post(f"{base}/api/copilot", json={"message": copilot_q}).json()
        print(copilot_q[:80] + "...")
        print("reply:", (cop.get("reply") or "")[:600])
        print("sources:", len(cop.get("sources") or []),
              "site=", sum(1 for s in cop.get("sources") or [] if s.get("kind") == "site"))
        print()

        # Create proposal
        print("--- create proposal ---")
        prop = client.post(f"{base}/api/proposals", json={
            "client_name": "Texas Tech University Health Sciences Center (TTUHSC)",
            "due_date": "2026-09-21",
        }).json()
        opp_id = prop["proposal_id"]
        print("opp_id:", opp_id)

        # Upload RFP
        print("--- upload RFP ---")
        up = client.post(
            f"{base}/api/opportunities/{opp_id}/documents",
            files={"file": (upload_name, rfp_bytes,
                              "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            data={"doc_role": "rfp"},
        )
        up.raise_for_status()
        doc = up.json()
        doc_id = doc["doc_id"]
        print("doc_id:", doc_id, "class:", doc.get("doc_class"))

        # Parse
        print("--- parse ---")
        parsed = client.post(f"{base}/api/rfp/parse", json={"doc_id": doc_id}).json()
        fields = parsed.get("parsed_fields") or {}
        print(json.dumps(fields, indent=2))
        print("matches:", len((parsed.get("match_data") or {}).get("matches") or []))

        # Shred
        print("--- shred ---")
        shred = client.post(f"{base}/api/opportunities/{opp_id}/shred",
                            json={"doc_id": doc_id}).json()
        reqs = shred.get("requirements") or []
        print(f"added {shred.get('added')} requirements; total extracted {len(reqs)}")
        for r in reqs[:5]:
            print(f"  [{r.get('module')}] {r.get('req_text','')[:90]}...")

        if args.skip_generate:
            print("\n=== smoke complete (generate skipped) ===")
            print(f"Studio URL: {base}/  proposal_id {opp_id}")
            return 0

        # Generate
        print("--- generate (async) ---")
        client.post(f"{base}/api/proposals/{opp_id}/generate").raise_for_status()
        for i in range(60):
            time.sleep(5)
            detail = client.get(f"{base}/api/proposals/{opp_id}").json()
            status = detail.get("gen_status") or detail.get("status")
            print(f"  poll {i+1}: gen_status={status}")
            if status not in ("generating",):
                break
        print("draft preview:", (detail.get("draft_text") or "")[:800])
        print("questionnaires:", len(detail.get("questionnaires") or []))

    print(f"\n=== live test complete — open {base}/ and load proposal {opp_id} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
