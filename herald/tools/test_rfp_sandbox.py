"""Sandbox run for one RFP through HAROLD generation paths.

Usage (from herald/):
  python tools/test_rfp_sandbox.py
  python tools/test_rfp_sandbox.py --live   # also create opp in DB + optional draft

Exercises parse → shred → sample matrix answer → sample narrative draft
without requiring the full Studio UI.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# TTUHSC RFP 739-SL3821039 — synthesized from HigherGov summary + typical RFP asks.
# Replace with full PDF text when documents are downloaded from Jaggaer.
SAMPLE_RFP = """
REQUEST FOR PROPOSALS
RFP 739-SL3821039
Consulting Services — Enterprise AI Adoption and Enablement

Issuing Agency: Texas Tech University Health Sciences Center (TTUHSC)
State: Texas
Response Deadline: September 21, 2026
Period of Performance: August 14, 2026 through September 21, 2026
Estimated Value: $150,000 - $900,000

1.0 BACKGROUND
TTUHSC seeks consulting services to support enterprise artificial intelligence adoption
and enablement across the organization. The contractor shall assess current AI
capabilities, develop a strategic integration plan, provide staff training, and
support implementation aligned with TTUHSC operational goals and health outcomes mission.

2.0 SCOPE OF WORK
2.1 The Offeror shall conduct a current-state assessment of AI readiness, data
governance, and existing technology platforms.
2.2 The Offeror shall develop a strategic roadmap for enterprise AI adoption including
governance, security, and change management.
2.3 The Offeror shall deliver workshops and training for TTUHSC staff on responsible
AI use, prompt engineering, and operational workflows.
2.4 The Offeror shall provide documentation of best practices and ongoing transition support.
2.5 The Offeror shall ensure all recommendations align with HIPAA, institutional policy,
and applicable Texas procurement requirements.

3.0 SUBMISSION REQUIREMENTS
3.1 Proposer must submit a HUB Subcontracting Plan with the proposal.
3.2 Proposer must include a signed W-9.
3.3 Proposer must provide a point of contact name, email, and notification address.
3.4 Proposer shall describe relevant experience with enterprise AI programs in
healthcare or higher-education settings.
3.5 Proposer shall provide a project schedule, staffing plan, and fixed-fee or
time-and-materials pricing consistent with the estimated value range.

4.0 EVALUATION CRITERIA
4.1 Understanding of TTUHSC mission and AI enablement goals.
4.2 Qualifications and experience of proposed personnel.
4.3 Methodology for assessment, roadmap development, training, and support.
4.4 Price reasonableness and value.
4.5 Compliance with HUB and administrative requirements.

Place of Performance: TTUHSC locations as required.
Questions: Jaggaer portal / 1-800-233-1121.
NAICS: 541611, 541512.
"""


async def run_sandbox(*, live: bool) -> int:
    from app import db, generation, llm, opportunities, site_grounding
    from app.config import cfg

    print("=== HAROLD RFP sandbox ===")
    print(f"client target: TTUHSC | RFP 739-SL3821039")
    print(f"site_grounding: {site_grounding.configured()}")
    print(f"auto_humanize: {cfg.auto_humanize}")
    print()

    await llm.startup()
    try:
        # 1) Parse fields
        print("--- parse_rfp ---")
        parsed = await generation.parse_rfp(SAMPLE_RFP)
        fields = parsed.get("parsed_fields") or {}
        print(json.dumps(fields, indent=2))
        print(f"past-win matches: {len(parsed.get('matches') or [])}")
        print()

        # 2) Shred requirements
        print("--- shred_requirements ---")
        reqs = await generation.shred_requirements(SAMPLE_RFP)
        print(f"extracted {len(reqs)} requirements")
        for r in reqs[:6]:
            print(f"  [{r.get('module')}|{r.get('response_type')}] {r.get('req_text','')[:100]}...")
        print()

        # 3) Sample matrix-style answer (TECH module)
        sample_q = (
            "Does the vendor provide enterprise AI governance frameworks, staff training, "
            "and HIPAA-aligned implementation support for healthcare organizations?"
        )
        print("--- answer_question (matrix row) ---")
        matrix = await generation.answer_question(
            sample_q,
            "TECH",
            ["Standard", "Configuration", "Modification", "Third Party", "Not Available"],
        )
        print(f"code: {matrix.get('response_code')}")
        print(f"confidence: {matrix.get('confidence')}")
        print(f"needs_human: {matrix.get('needs_human')}")
        print(f"sources: {len(matrix.get('sources') or [])} "
              f"(site={sum(1 for s in matrix.get('sources') or [] if s.get('kind')=='site')})")
        print(f"text: {matrix.get('response_text','')[:500]}...")
        print()

        # 4) Sample narrative draft
        if reqs:
            req = reqs[0]
            print("--- draft_requirement (first shredded req) ---")
            draft = await generation.draft_requirement(
                req["req_text"],
                req.get("module") or "GENERAL",
                fields.get("client_name") or "Texas Tech University Health Sciences Center",
                state="Texas",
            )
            body = draft.get("final") or draft.get("draft") or ""
            print(f"sources: {len(draft.get('sources') or [])}")
            print(body[:800] + ("..." if len(body) > 800 else ""))
            print()

        if live:
            print("--- live DB: create opportunity ---")
            db.init_pool()
            opp_id = opportunities.create({
                "client_name": fields.get("client_name") or "TTUHSC",
                "agency": fields.get("agency") or "Texas Tech University Health Sciences Center",
                "title": "RFP 739-SL3821039 — Enterprise AI Adoption",
                "due_date": fields.get("due_date") or "2026-09-21",
                "solicitation_no": fields.get("rfp_number") or "RFP 739-SL3821039",
                "parsed_fields": fields,
                "match_data": {"matches": parsed.get("matches") or []},
            }, "sandbox-test")
            print(f"created opp_id={opp_id}")
            if reqs:
                from app import classifier
                with db.transaction() as conn:
                    cur = conn.cursor()
                    for r in reqs[:10]:
                        cur.execute(
                            """INSERT INTO harald_requirements
                                 (opp_id, rfp_ref, req_text, module_tag, mandatory,
                                  response_type, status)
                               VALUES (:o, :ref, :txt, :mod, :mand, :rtype, 'not_started')""",
                            {
                                "o": opp_id,
                                "ref": r.get("rfp_ref") or "",
                                "txt": r.get("req_text"),
                                "mod": r.get("module") or "GENERAL",
                                "mand": r.get("mandatory") or "N",
                                "rtype": r.get("response_type") or "narrative",
                            },
                        )
                print(f"inserted {min(len(reqs), 10)} requirements")
            print("Run Studio generate on this opp_id in the UI when ready.")
    finally:
        await llm.shutdown()
        db.close_pool()

    print("\n=== sandbox complete ===")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="Create opp in Oracle (needs wallet)")
    args = ap.parse_args()
    return asyncio.run(run_sandbox(live=args.live))


if __name__ == "__main__":
    raise SystemExit(main())
