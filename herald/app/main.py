"""HARALD API.

Serves the FRP Studio and the Bids & Compliance, Answer Library, Packages, and
Admin workspaces, and exposes the engine behind them.

OCI Generative AI is the only model service. Embeddings run locally in this
container, so retrieval costs nothing per call and works with no network.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, File, Form, Header, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import (answers, audit, auth, db, documents, embeddings, formats, freshness,
               generation, llm, opportunities, packages, pricing, questionnaires,
               reviews, studio)
from .config import cfg
from .errors import HaraldError, ValidationFailed

logging.basicConfig(
    level=getattr(logging, cfg.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
)
log = logging.getLogger("harald")

WEB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")


@asynccontextmanager
async def lifespan(_: FastAPI):
    cfg.validate()
    db.init_pool()
    await llm.startup()
    # Load the embedding model off the event loop so startup does not block it.
    await asyncio.get_running_loop().run_in_executor(None, embeddings.model)
    log.info("HARALD ready: draft=%s polish=%s embed=%s",
             cfg.draft_model, cfg.polish_model, cfg.embed_model)
    yield
    await llm.shutdown()
    db.close_pool()


app = FastAPI(title="HARALD", version="1.0.0", lifespan=lifespan)


@app.exception_handler(HaraldError)
async def harald_error_handler(_, exc: HaraldError):
    if exc.status >= 500:
        log.error("%s: %s", exc.code, exc.message, exc_info=exc)
    return JSONResponse(status_code=exc.status, content=exc.to_dict())


@app.exception_handler(Exception)
async def unhandled_handler(_, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse(status_code=500,
                        content={"error": "internal_error", "message": str(exc), "detail": {}})


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def identity(x_harald_token: str | None = Header(default=None)) -> dict:
    """No login wall for drafting. Missing/bad tokens use the shared Studio identity."""
    if not x_harald_token:
        return dict(auth.SHARED_WORKSPACE)
    try:
        return auth.parse_token(x_harald_token)
    except Exception:
        return dict(auth.SHARED_WORKSPACE)


def contributor(user: dict = Depends(identity)) -> dict:
    return auth.require(user, auth.CONTRIBUTOR)


def reviewer_role(user: dict = Depends(identity)) -> dict:
    return auth.require(user, auth.REVIEWER)


def approver_role(user: dict = Depends(identity)) -> dict:
    return auth.require_approver(user)


class SignIn(BaseModel):
    username: str
    passphrase: str | None = None


@app.get("/api/users")
def users():
    return auth.list_users()


@app.post("/api/signin")
def signin(body: SignIn):
    return auth.sign_in(body.username, body.passphrase)


@app.get("/api/me")
def me(user: dict = Depends(identity)):
    return user


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    database_ok = db.healthcheck()
    body = {"ok": database_ok, "database": "up" if database_ok else "down",
            "draft_model": cfg.draft_model, "polish_model": cfg.polish_model,
            "embed_model": cfg.embed_model, "embed_dim": cfg.embed_dim}
    if database_ok:
        body["library"] = documents.library_stats()
        body["answers"] = answers.stats()
    return JSONResponse(status_code=200 if database_ok else 503, content=body)


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------
@app.get("/api/library")
def library(status: str | None = None, q: str | None = None,
            limit: int = Query(300, le=1000)):
    outcome = status if status not in (None, "all", "proposals") else None
    return {"docs": documents.list_documents(outcome=outcome, query=q, limit=limit)}


@app.get("/api/library/stats")
def library_stats():
    return documents.library_stats()


@app.post("/api/library/upload")
async def library_upload(file: UploadFile = File(...),
                         deal_status: str = Form("in_progress"),
                         client: str | None = Form(None),
                         user: dict = Depends(contributor)):
    data = await file.read()
    result = documents.store(file.filename, data, client_name=client,
                             outcome=deal_status, actor=user["username"])
    audit.record(user["username"], "library.upload", "document", result["doc_id"],
                 {"file": file.filename, "class": result["doc_class"]})
    return result


@app.get("/api/docs/{doc_id}")
def get_doc(doc_id: int):
    return documents.get(doc_id)


@app.get("/api/documents/{doc_id}/download")
def download_doc(doc_id: int):
    blob, filename = documents.get_blob(doc_id)
    return Response(content=blob, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


class Promote(BaseModel):
    client: str | None = None
    outcome: str = "won"


@app.post("/api/documents/{doc_id}/promote")
def promote_doc(doc_id: int, body: Promote, user: dict = Depends(contributor)):
    result = documents.promote(doc_id, client_name=body.client, outcome=body.outcome)
    audit.record(user["username"], "library.promote", "document", doc_id, result)
    return result


# ---------------------------------------------------------------------------
# Opportunities and the requirements traceability matrix
# ---------------------------------------------------------------------------
class OpportunityIn(BaseModel):
    client_name: str | None = None
    agency: str | None = None
    solicitation_no: str | None = None
    title: str | None = None
    due_date: str | None = None
    status: str | None = None
    bid_decision: str | None = None
    portal_url: str | None = None
    format_profile_id: int | None = None


@app.get("/api/opportunities")
def list_opportunities():
    return opportunities.list_all()


@app.post("/api/opportunities")
def create_opportunity(body: OpportunityIn, user: dict = Depends(contributor)):
    return {"opp_id": opportunities.create(body.model_dump(exclude_none=True),
                                           user["username"])}


@app.get("/api/opportunities/{opp_id}")
def get_opportunity(opp_id: int):
    return opportunities.get(opp_id)


@app.patch("/api/opportunities/{opp_id}")
def patch_opportunity(opp_id: int, body: dict = Body(...), user: dict = Depends(contributor)):
    opportunities.update(opp_id, body, user["username"])
    return {"ok": True}


@app.get("/api/opportunities/{opp_id}/rollup")
def rollup(opp_id: int):
    return opportunities.compliance(opp_id)


@app.post("/api/opportunities/{opp_id}/documents")
async def add_opportunity_document(opp_id: int, file: UploadFile = File(...),
                                   doc_role: str = Form("reference"),
                                   effective_date: str | None = Form(None),
                                   user: dict = Depends(contributor)):
    data = await file.read()
    opp = opportunities.get(opp_id)
    result = documents.store(
        file.filename, data, opp_id=opp_id, doc_role=doc_role,
        doc_class="CLIENT_RFP" if doc_role in ("rfp", "addendum") else None,
        client_name=opp["client_name"], effective_date=effective_date,
        actor=user["username"])
    if doc_role == "rfp" and not opp["rfp_doc_id"]:
        opportunities.update(opp_id, {"rfp_doc_id": result["doc_id"]}, user["username"])
    audit.record(user["username"], "opportunity.document.add", "opportunity", opp_id,
                 {"file": file.filename, "role": doc_role})
    return result


class DocRef(BaseModel):
    doc_id: int


@app.post("/api/opportunities/{opp_id}/shred")
async def shred(opp_id: int, body: DocRef, user: dict = Depends(contributor)):
    text = documents.get_text(body.doc_id)
    if not text.strip():
        raise ValidationFailed(
            "That document has no extractable text, so requirements cannot be read from "
            "it. It may be a scan; upload a text-based version.")
    reqs = await generation.shred_requirements(text)
    added = opportunities.add_requirements(opp_id, reqs, source_doc_id=body.doc_id,
                                           actor=user["username"])
    return {"added": added, "requirements": reqs}


@app.post("/api/opportunities/{opp_id}/amendment")
async def amendment(opp_id: int, file: UploadFile = File(...),
                    effective_date: str | None = Form(None),
                    user: dict = Depends(contributor)):
    data = await file.read()
    return await opportunities.load_amendment(opp_id, file.filename, data,
                                              effective_date, user["username"])


class RequirementIn(BaseModel):
    req_text: str = Field(min_length=1)
    rfp_ref: str | None = None
    module: str = "GENERAL"
    mandatory: str = "N"
    response_type: str = "narrative"


@app.post("/api/opportunities/{opp_id}/requirements")
def add_requirement(opp_id: int, body: RequirementIn, user: dict = Depends(contributor)):
    opportunities.add_requirements(opp_id, [body.model_dump()], actor=user["username"])
    return {"ok": True}


@app.patch("/api/requirements/{req_id}")
def patch_requirement(req_id: int, body: dict = Body(...), user: dict = Depends(contributor)):
    opportunities.update_requirement(req_id, body, user["username"])
    return {"ok": True}


@app.post("/api/requirements/{req_id}/draft")
async def draft_requirement(req_id: int, user: dict = Depends(contributor)):
    req = opportunities.requirement(req_id)
    result = await generation.draft_requirement(req["req_text"], req["module_tag"],
                                                req["client"])
    opportunities.save_draft(req_id, result["draft"], result["sources"])
    return result


class HumanizeIn(BaseModel):
    draft: str


@app.post("/api/requirements/{req_id}/humanize")
async def humanize_requirement(req_id: int, body: HumanizeIn,
                               user: dict = Depends(contributor)):
    req = opportunities.requirement(req_id)
    final = await generation.humanize(body.draft, req["client"])
    opportunities.save_draft(req_id, body.draft, None, final=final)
    return {"final": final}


@app.post("/api/opportunities/{opp_id}/generate")
async def generate_all(opp_id: int, user: dict = Depends(contributor)):
    opportunities.set_generation_state(opp_id, "generating")
    asyncio.create_task(opportunities.generate_narrative(opp_id, user["username"]))
    return {"status": "generating"}


# ---------------------------------------------------------------------------
# Answer library
# ---------------------------------------------------------------------------
@app.get("/api/answers")
def list_answers(status: str | None = None, module: str | None = None,
                 q: str | None = None, limit: int = Query(200, le=500)):
    return answers.list_answers(status, module, q, limit)


@app.get("/api/answers/stats")
def answer_stats():
    return answers.stats()


class AnswerIn(BaseModel):
    question_canonical: str
    answer_text: str
    module_tag: str = "GENERAL"
    tags: str | None = None
    owner_sme: str | None = None
    status: str = "draft"
    effective_date: str | None = None
    review_due: str | None = None
    source_refs: str | None = None


@app.post("/api/answers")
def create_answer(body: AnswerIn, user: dict = Depends(contributor)):
    ans_id = answers.create(body.model_dump(exclude_none=True), user["username"])
    audit.record(user["username"], "answer.create", "answer", ans_id)
    return {"ans_id": ans_id}


@app.get("/api/answers/{ans_id}")
def get_answer(ans_id: int):
    return answers.get(ans_id)


@app.patch("/api/answers/{ans_id}")
def patch_answer(ans_id: int, body: dict = Body(...), user: dict = Depends(contributor)):
    if body.get("status") == "approved":
        auth.require(user, auth.REVIEWER)
    answers.update(ans_id, body)
    audit.record(user["username"], "answer.update", "answer", ans_id, {"fields": list(body)})
    return {"ok": True}


class ReviewedIn(BaseModel):
    months: int = 6


@app.post("/api/answers/{ans_id}/reviewed")
def answer_reviewed(ans_id: int, body: ReviewedIn, user: dict = Depends(reviewer_role)):
    return freshness.mark_reviewed(ans_id, body.months, user["username"])


# ---------------------------------------------------------------------------
# Excel questionnaires
# ---------------------------------------------------------------------------
@app.get("/api/opportunities/{opp_id}/questionnaires")
def list_questionnaires(opp_id: int):
    return questionnaires.list_for_opportunity(opp_id)


@app.post("/api/opportunities/{opp_id}/questionnaires/import")
def import_questionnaire(opp_id: int, body: DocRef, user: dict = Depends(contributor)):
    return questionnaires.import_workbook(opp_id, body.doc_id, user["username"])


@app.get("/api/questionnaires/{q_id}")
def get_questionnaire(q_id: int):
    return questionnaires.get(q_id)


@app.post("/api/questionnaires/{q_id}/fill")
async def fill_questionnaire(q_id: int, user: dict = Depends(contributor)):
    questionnaires.set_status(q_id, "filling")
    asyncio.create_task(questionnaires.fill(q_id, user["username"]))
    return {"status": "filling"}


@app.patch("/api/questionnaire-items/{qi_id}")
def patch_questionnaire_item(qi_id: int, body: dict = Body(...),
                             user: dict = Depends(contributor)):
    questionnaires.update_item(qi_id, body, user["username"])
    return {"ok": True}


@app.get("/api/questionnaires/{q_id}/export")
def export_questionnaire(q_id: int):
    data, filename = questionnaires.export(q_id)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------------------
# Format profiles
# ---------------------------------------------------------------------------
@app.get("/api/formats")
def list_formats():
    return formats.list_profiles()


@app.get("/api/formats/{profile_id}")
def get_format(profile_id: int):
    return formats.get(profile_id)


@app.post("/api/formats")
def create_format(body: dict = Body(...), user: dict = Depends(reviewer_role)):
    profile_id = formats.create(body, user["username"])
    audit.record(user["username"], "format.create", "format", profile_id)
    return {"profile_id": profile_id}


@app.patch("/api/formats/{profile_id}")
def patch_format(profile_id: int, body: dict = Body(...), user: dict = Depends(reviewer_role)):
    formats.update(profile_id, body)
    audit.record(user["username"], "format.update", "format", profile_id)
    return {"ok": True}


class CloneFormat(BaseModel):
    name: str
    agency: str | None = None


@app.post("/api/formats/{profile_id}/clone")
def clone_format(profile_id: int, body: CloneFormat, user: dict = Depends(reviewer_role)):
    return {"profile_id": formats.clone(profile_id, body.name, body.agency)}


# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------
@app.get("/api/opportunities/{opp_id}/packages")
def list_packages(opp_id: int):
    return packages.list_for_opportunity(opp_id)


@app.post("/api/opportunities/{opp_id}/packages")
async def assemble_package(opp_id: int, user: dict = Depends(contributor)):
    return await packages.assemble(opp_id, user["username"])


@app.get("/api/packages/{package_id}")
def get_package(package_id: int):
    return packages.get(package_id)


class SectionIn(BaseModel):
    body: str


@app.patch("/api/package-sections/{section_id}")
def patch_section(section_id: int, body: SectionIn, user: dict = Depends(contributor)):
    packages.update_section(section_id, body.body, user["username"])
    return {"ok": True}


@app.post("/api/packages/{package_id}/render")
def render_package(package_id: int, user: dict = Depends(contributor)):
    return packages.render(package_id)


@app.get("/api/packages/{package_id}/download")
def download_package(package_id: int, kind: str = Query("docx", pattern="^(docx|pdf)$")):
    blob, filename, media = packages.download(package_id, kind)
    return Response(content=blob, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/api/packages/{package_id}/approve")
def approve_package(package_id: int, user: dict = Depends(approver_role)):
    return packages.approve(package_id, user["username"])


@app.post("/api/packages/{package_id}/submit")
def submit_package(package_id: int, user: dict = Depends(approver_role)):
    return packages.submit(package_id, user["username"])


# ---------------------------------------------------------------------------
# Review gates
# ---------------------------------------------------------------------------
class GateIn(BaseModel):
    gate: str
    reviewer: str | None = None


@app.get("/api/packages/{package_id}/reviews")
def package_reviews(package_id: int):
    return reviews.for_package(package_id)


@app.post("/api/packages/{package_id}/reviews")
def open_review(package_id: int, body: GateIn, user: dict = Depends(contributor)):
    return reviews.open_gate(package_id, body.gate, body.reviewer, user["username"])


class DecisionIn(BaseModel):
    status: str
    comments: str | None = None


@app.post("/api/reviews/{review_id}/decide")
def decide_review(review_id: int, body: DecisionIn, user: dict = Depends(reviewer_role)):
    return reviews.decide(review_id, body.status, user, body.comments)


# ---------------------------------------------------------------------------
# Pricing. Approver only, on every route.
# ---------------------------------------------------------------------------
@app.get("/api/opportunities/{opp_id}/pricing")
def pricing_history(opp_id: int, user: dict = Depends(approver_role)):
    return {"current": pricing.current(opp_id), "history": pricing.history(opp_id)}


@app.post("/api/opportunities/{opp_id}/pricing")
async def upload_pricing(opp_id: int, file: UploadFile = File(...),
                         notes: str | None = Form(None),
                         user: dict = Depends(approver_role)):
    data = await file.read()
    return pricing.upload(opp_id, file.filename, data, user["username"], notes)


class PricingStatus(BaseModel):
    status: str


@app.post("/api/pricing/{price_id}/status")
def pricing_status(price_id: int, body: PricingStatus, user: dict = Depends(approver_role)):
    return pricing.set_status(price_id, body.status, user["username"])


class PricingLock(BaseModel):
    locked: bool


@app.post("/api/pricing/{price_id}/lock")
def pricing_lock(price_id: int, body: PricingLock, user: dict = Depends(approver_role)):
    return pricing.set_lock(price_id, body.locked, user["username"])


@app.get("/api/pricing/{price_id}/download")
def pricing_download(price_id: int, user: dict = Depends(approver_role)):
    blob, filename = pricing.download(price_id)
    return Response(content=blob, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------
@app.get("/api/freshness/queue")
def freshness_queue():
    return freshness.review_queue()


@app.get("/api/freshness/notes")
def freshness_notes():
    return freshness.list_notes()


@app.post("/api/freshness/notes")
async def freshness_upload(file: UploadFile = File(...),
                           release_version: str | None = Form(None),
                           user: dict = Depends(contributor)):
    data = await file.read()
    return freshness.ingest_release_document(file.filename, data,
                                             release_version=release_version,
                                             actor=user["username"])


@app.post("/api/freshness/notes/{note_id}/assess")
async def freshness_assess(note_id: int, user: dict = Depends(contributor)):
    return await freshness.assess_impact(note_id, user["username"])


# ---------------------------------------------------------------------------
# Assistant
# ---------------------------------------------------------------------------
class ChatIn(BaseModel):
    question: str = Field(min_length=1)


@app.post("/api/chat")
async def chat(body: ChatIn):
    return await generation.chat(body.question)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
@app.get("/api/audit")
def audit_trail(entity_type: str | None = None, entity_id: int | None = None,
                limit: int = Query(100, le=500), user: dict = Depends(reviewer_role)):
    return audit.trail(entity_type, entity_id, limit)


# ---------------------------------------------------------------------------
# FRP Studio bridge contract
# ---------------------------------------------------------------------------
@app.post("/api/proposals")
def studio_create(body: dict = Body(...), user: dict = Depends(contributor)):
    return studio.create_proposal(body, user["username"])


@app.get("/api/proposals")
def studio_list(limit: int = Query(100, le=200)):
    return {"proposals": studio.list_proposals(limit)}


@app.get("/api/proposals/{opp_id}")
def studio_get(opp_id: int):
    return studio.get_proposal(opp_id)


@app.put("/api/proposals/{opp_id}")
def studio_update(opp_id: int, body: dict = Body(...), user: dict = Depends(contributor)):
    return studio.update_proposal(opp_id, body, user["username"])


class AttachIn(BaseModel):
    doc_id: int
    role: str | None = "reference"


@app.post("/api/proposals/{opp_id}/attach")
def studio_attach(opp_id: int, body: AttachIn, user: dict = Depends(contributor)):
    return studio.attach(opp_id, body.doc_id, body.role, user["username"])


@app.post("/api/proposals/{opp_id}/generate")
async def studio_generate(opp_id: int, user: dict = Depends(contributor)):
    opportunities.set_generation_state(opp_id, "generating")
    asyncio.create_task(studio.generate(opp_id, user["username"]))
    return {"status": "generating"}


@app.get("/api/proposals/{opp_id}/export.docx")
def studio_export_docx(opp_id: int):
    blob, filename = studio.export_docx(opp_id)
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/proposals/{opp_id}/materials.zip")
def studio_export_materials(opp_id: int):
    """Word draft + filled agency spreadsheets + attachments (+ packages if any)."""
    blob, filename = studio.export_materials_zip(opp_id)
    return Response(
        content=blob,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/rfp/parse")
async def studio_parse(body: DocRef):
    return await studio.parse(body.doc_id)


class CopilotIn(BaseModel):
    message: str
    conversationId: str | None = None


@app.post("/api/copilot")
async def studio_copilot(body: CopilotIn):
    result = await generation.chat(body.message)
    return {"reply": result["answer"], "conversation_id": body.conversationId or "c1",
            "sources": result["sources"]}


# ---------------------------------------------------------------------------
# Static workspaces
# ---------------------------------------------------------------------------
def _page(name: str) -> FileResponse:
    return FileResponse(os.path.join(WEB, name))


@app.get("/")
def studio_ui():
    return _page("index.html")


@app.get("/opportunities")
def opportunities_ui():
    return _page("opportunities.html")


@app.get("/answers")
def answers_ui():
    return _page("answers.html")


@app.get("/packages")
def packages_ui():
    return _page("packages.html")


@app.get("/admin")
def admin_ui():
    return _page("admin.html")


@app.get("/frp-rest-bridge.js")
def bridge():
    return FileResponse(os.path.join(WEB, "frp-rest-bridge.js"),
                        media_type="application/javascript")


@app.get("/harald.js")
def harald_js():
    return FileResponse(os.path.join(WEB, "harald.js"), media_type="application/javascript")


@app.get("/harald.css")
def harald_css():
    return FileResponse(os.path.join(WEB, "harald.css"), media_type="text/css")
