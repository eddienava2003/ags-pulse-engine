import io
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath

from fastapi import APIRouter, Header, HTTPException, Request
from openai import OpenAI
from supabase import create_client

logger = logging.getLogger("ags-pulse.documents")
router = APIRouter(prefix="/trade-documents", tags=["Trade Documents"])

BUCKET = "ags-trade-documents"
MAX_FILE_BYTES = 25 * 1024 * 1024
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "text/plain",
    "image/png",
    "image/jpeg",
}
DOCUMENT_TYPES = {
    "NDA", "NNN", "MSA", "SUPPLIER_CONTRACT", "COMMERCIAL_INVOICE",
    "PACKING_LIST", "BOL_DRAFT", "CERTIFICATE_OF_ORIGIN",
    "SUPPLIER_QUOTE", "COMPLIANCE_CERTIFICATE", "OTHER",
}
RISK_LEVELS = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


def _client():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise HTTPException(status_code=503, detail="Supabase configuration is missing.")
    return create_client(url, key)


def _auth_context(authorization: str, requested_org: str = ""):
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="A valid Bearer access token is required.")
    db = _client()
    try:
        auth_user = db.auth.get_user(token.strip()).user
        user_id = str(auth_user.id)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired access token.")
    try:
        memberships = (
            db.table("organization_memberships")
            .select("organization_id")
            .eq("user_id", user_id)
            .execute()
        ).data or []
    except Exception:
        logger.exception("Document organization membership lookup failed.")
        raise HTTPException(status_code=503, detail="Organization access is temporarily unavailable.")
    orgs = list(dict.fromkeys(str(row["organization_id"]) for row in memberships if row.get("organization_id")))
    requested = (requested_org or "").strip()
    if requested:
        if requested not in orgs:
            raise HTTPException(status_code=403, detail="You are not a member of this organization.")
        return db, user_id, requested
    if not orgs:
        raise HTTPException(status_code=403, detail="No organization membership is assigned to this account.")
    if len(orgs) != 1:
        raise HTTPException(status_code=400, detail="Select an organization to continue.")
    return db, user_id, orgs[0]


def _document(db, document_id: str, organization_id: str):
    rows = (
        db.table("trade_documents").select("*")
        .eq("id", document_id).eq("organization_id", organization_id)
        .limit(1).execute()
    ).data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Document not found.")
    return rows[0]


def _ai_json(system_prompt: str, user_payload: dict):
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="AI document services are not configured.")
    model = os.getenv("OPENAI_MODEL", "gpt-5.5").strip()
    try:
        response = OpenAI(api_key=api_key).responses.create(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, default=str)},
            ],
            text={"format": {"type": "json_object"}},
        )
        result = json.loads(response.output_text)
        if not isinstance(result, dict):
            raise ValueError("AI response must be a JSON object.")
        return result, model
    except HTTPException:
        raise
    except Exception:
        logger.exception("AI document operation failed.")
        raise HTTPException(status_code=502, detail="AI document service could not complete the request.")


def _safe_filename(value: str, extension: str):
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", value or "document").strip("-._")[:90] or "document"
    suffix = re.sub(r"[^A-Za-z0-9]+", "", extension or "bin").lower()[:8] or "bin"
    return f"{base}.{suffix}"


def _valid_file_signature(mime: str, data: bytes) -> bool:
    signatures = {
        "application/pdf": data.startswith(b"%PDF-"),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": data.startswith(bytes.fromhex("504B0304")),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": data.startswith(bytes.fromhex("504B0304")),
        "application/msword": data.startswith(bytes.fromhex("D0CF11E0A1B11AE1")),
        "application/vnd.ms-excel": data.startswith(bytes.fromhex("D0CF11E0A1B11AE1")),
        "image/png": data.startswith(bytes.fromhex("89504E470D0A1A0A")),
        "image/jpeg": data.startswith(bytes.fromhex("FFD8FF")),
        "text/plain": bytes([0]) not in data,
    }
    return signatures.get(mime, False)


def _hs_code_format_check(value: str):
    code = re.sub(r"[ .-]", "", str(value or ""))
    if not code.isdigit():
        return {"valid_format": False, "normalized_code": code, "message": "HS code must contain digits only."}
    if len(code) not in {6, 8, 10}:
        return {"valid_format": False, "normalized_code": code, "message": "HS code must contain 6, 8, or 10 digits."}
    return {
        "valid_format": True,
        "normalized_code": code,
        "hs_level": {6: "international_harmonized", 8: "national_or_regional", 10: "national_tariff_line"}[len(code)],
        "message": "Format accepted. Official tariff schedule and product classification have not been verified.",
    }


@router.get("/customs/orders/{order_id}/readiness")
def get_customs_document_readiness(
    order_id: str,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default=""),
):
    db, _, org_id = _auth_context(authorization, x_organization_id)
    orders = (
        db.table("trade_orders").select("*").eq("id", order_id)
        .eq("organization_id", org_id).limit(1).execute()
    ).data or []
    if not orders:
        raise HTTPException(status_code=404, detail="Trade order not found.")
    order = orders[0]
    items = (
        db.table("trade_order_items").select("*").eq("order_id", order_id)
        .eq("organization_id", org_id).execute()
    ).data or []
    documents = (
        db.table("trade_documents").select("id,document_type,status,title")
        .eq("order_id", order_id).eq("organization_id", org_id).execute()
    ).data or []

    missing_order_fields = [
        field for field in ("buyer_name", "buyer_country", "seller_name", "seller_country", "currency_code", "incoterms")
        if not str(order.get(field) or "").strip()
    ]
    item_checks = []
    for item in items:
        missing = [
            field for field in ("description", "hs_code", "country_of_origin", "quantity", "unit_of_measure", "unit_price")
            if item.get(field) is None or (isinstance(item.get(field), str) and not item.get(field).strip())
        ]
        hs_check = _hs_code_format_check(item.get("hs_code") or "")
        item_checks.append({
            "item_id": item.get("id"),
            "description": item.get("description"),
            "missing_fields": missing,
            "hs_code_format": hs_check,
            "complete": not missing and hs_check["valid_format"],
        })

    approved_types = {row.get("document_type") for row in documents if row.get("status") == "APPROVED"}
    required_documents = _required_customs_document_types(order)
    missing_documents = [doc_type for doc_type in required_documents if doc_type not in approved_types]

    blockers = []
    if missing_order_fields:
        blockers.append({"category": "ORDER_DATA", "missing_fields": missing_order_fields})
    if not items:
        blockers.append({"category": "LINE_ITEMS", "message": "At least one trade order item is required."})
    for check in item_checks:
        if not check["complete"]:
            blockers.append({"category": "LINE_ITEM", "item_id": check["item_id"], "missing_fields": check["missing_fields"], "hs_code_format": check["hs_code_format"]})
    if missing_documents:
        blockers.append({"category": "DOCUMENTS", "missing_approved_documents": missing_documents})

    return {
        "success": True,
        "order_id": order_id,
        "readiness": "READY_FOR_HUMAN_CUSTOMS_REVIEW" if not blockers else "BLOCKED",
        "blocker_count": len(blockers),
        "blockers": blockers,
        "line_items": item_checks,
        "document_statuses": documents,
        "important_notice": "Readiness checks completeness and HS-code format only. It does not verify tariff classification, duty rates, origin eligibility, sanctions, or customs compliance.",
    }


def _required_customs_document_types(order: dict):
    required = ["COMMERCIAL_INVOICE", "PACKING_LIST"]
    buyer = str(order.get("buyer_country") or "").strip().casefold()
    seller = str(order.get("seller_country") or "").strip().casefold()
    if buyer and seller and buyer != seller:
        required.append("CERTIFICATE_OF_ORIGIN")
    return required


@router.post("/customs/orders/{order_id}/prepare", status_code=201)
def prepare_customs_document_packet(
    order_id: str,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default=""),
):
    db, user_id, org_id = _auth_context(authorization, x_organization_id)
    orders = (
        db.table("trade_orders").select("*").eq("id", order_id)
        .eq("organization_id", org_id).limit(1).execute()
    ).data or []
    if not orders:
        raise HTTPException(status_code=404, detail="Trade order not found.")
    order = orders[0]
    items = (
        db.table("trade_order_items").select("*").eq("order_id", order_id)
        .eq("organization_id", org_id).execute()
    ).data or []
    existing = (
        db.table("trade_documents").select("id,document_type,title,status,version")
         .eq("order_id", order_id).eq("organization_id", org_id).order("created_at", desc=True).execute()
    ).data or []
    active_by_type = {}
    for row in existing:
        doc_type = row.get("document_type")
        if doc_type and row.get("status") in {"DRAFT", "PENDING_REVIEW", "APPROVED"}:
            active_by_type.setdefault(doc_type, row)
    packet = []
    created = []
    for doc_type in _required_customs_document_types(order):
        if doc_type in active_by_type:
            packet.append(active_by_type[doc_type])
            continue
        title = doc_type.replace("_", " ").title()
        seed = {
            "workflow": "customs_document_packet",
            "source_order_snapshot": order,
            "source_line_items_snapshot": items,
            "human_review_required": True,
            "ai_draft_required": True,
        }
        try:
            rows = db.table("trade_documents").insert({
                "organization_id": org_id,
                "order_id": order_id,
                "document_type": doc_type,
                "title": title,
                "status": "DRAFT",
                "structured_data": seed,
                "created_by": user_id,
            }).execute().data or []
            if not rows:
                raise RuntimeError("Document insert returned no row.")
            packet.append(rows[0])
            created.append(rows[0])
        except Exception:
            logger.exception("Customs document packet creation failed for order %s type %s", order_id, doc_type)
            raise HTTPException(status_code=503, detail="Customs document packet could not be created completely.")
    return {
        "success": True,
        "order_id": order_id,
        "required_document_types": _required_customs_document_types(order),
        "created_count": len(created),
        "documents": packet,
        "next_step": "Draft each DRAFT document using POST /trade-documents/draft with its document_id, then review and obtain human approval.",
        "important_notice": "Packet preparation creates draft records and copies order data as a starting snapshot. It does not create filing-ready documents or certify customs compliance.",
    }


@router.post("/customs/hs-code/validate")
def validate_customs_hs_code(
    payload: dict,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default=""),
):
    _db, _, _org_id = _auth_context(authorization, x_organization_id)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="A JSON object is required.")
    code_result = _hs_code_format_check(payload.get("hs_code") or "")
    description = str(payload.get("product_description") or "").strip()
    import_country = str(payload.get("import_country") or "").strip()
    origin_country = str(payload.get("country_of_origin") or "").strip()
    if not description:
        raise HTTPException(status_code=422, detail="product_description is required.")
    if not import_country:
        raise HTTPException(status_code=422, detail="import_country is required.")
    return {
        "success": True,
        **code_result,
        "product_description_provided": True,
        "import_country": import_country,
        "country_of_origin": origin_country or None,
        "classification_status": "NOT_VERIFIED",
        "tariff_schedule_status": "NOT_QUERIED",
        "human_customs_review_required": True,
    }


@router.get("")
def list_trade_documents(
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default=""),
    order_id: str = "",
    status: str = "",
):
    db, _, org_id = _auth_context(authorization, x_organization_id)
    try:
        query = db.table("trade_documents").select(
            "id,organization_id,order_id,document_type,title,status,storage_path,mime_type,version,created_at,updated_at"
        ).eq("organization_id", org_id)
        if order_id:
            query = query.eq("order_id", order_id)
        if status:
            query = query.eq("status", status.upper())
        rows = query.order("created_at", desc=True).limit(100).execute().data or []
        return {"success": True, "count": len(rows), "documents": rows}
    except Exception:
        logger.exception("Trade document listing failed.")
        raise HTTPException(status_code=503, detail="Documents could not be loaded.")


@router.post("", status_code=201)
def create_trade_document(
    payload: dict,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default=""),
):
    db, user_id, org_id = _auth_context(authorization, x_organization_id)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="A JSON object is required.")
    doc_type = str(payload.get("document_type") or "OTHER").strip().upper()
    if doc_type not in DOCUMENT_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported document_type.")
    title = str(payload.get("title") or "").strip()[:200]
    if not title:
        raise HTTPException(status_code=422, detail="title is required.")
    order_id = str(payload.get("order_id") or "").strip() or None
    if order_id:
        parent = (
            db.table("trade_orders").select("id").eq("id", order_id)
            .eq("organization_id", org_id).limit(1).execute()
        ).data or []
        if not parent:
            raise HTTPException(status_code=404, detail="Trade order not found in this organization.")
    try:
        row = db.table("trade_documents").insert({
            "organization_id": org_id,
            "order_id": order_id,
            "document_type": doc_type,
            "title": title,
            "status": "DRAFT",
            "structured_data": payload.get("structured_data") if isinstance(payload.get("structured_data"), dict) else {},
            "created_by": user_id,
        }).execute().data
        if not row:
            raise RuntimeError("Document insert returned no row.")
        return {"success": True, "document": row[0]}
    except Exception:
        logger.exception("Trade document record creation failed.")
        raise HTTPException(status_code=503, detail="Document record could not be created.")


@router.get("/{document_id}")
def get_trade_document(
    document_id: str,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default=""),
):
    db, _, org_id = _auth_context(authorization, x_organization_id)
    document = _document(db, document_id, org_id)
    reviews = (
        db.table("trade_document_reviews").select("*")
        .eq("document_id", document_id).eq("organization_id", org_id)
        .order("created_at", desc=True).limit(10).execute()
    ).data or []
    return {"success": True, "document": document, "reviews": reviews}


@router.post("/draft", status_code=201)
def draft_trade_document(
    payload: dict,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default=""),
):
    db, user_id, org_id = _auth_context(authorization, x_organization_id)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="A JSON object is required.")
    doc_type = str(payload.get("document_type") or "").strip().upper()
    if doc_type not in DOCUMENT_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported document_type.")
    title = str(payload.get("title") or doc_type.replace("_", " ").title()).strip()[:200]
    instructions = str(payload.get("instructions") or "").strip()[:6000]
    order_id = str(payload.get("order_id") or "").strip() or None
    target_document_id = str(payload.get("document_id") or "").strip() or None
    target_document = _document(db, target_document_id, org_id) if target_document_id else None
    if target_document:
        if target_document.get("document_type") != doc_type:
            raise HTTPException(status_code=409, detail="document_id document_type does not match the requested draft type.")
        if target_document.get("status") not in {"DRAFT", "REJECTED"}:
            raise HTTPException(status_code=409, detail="Only DRAFT or REJECTED documents can be re-drafted.")
        existing_order_id = str(target_document.get("order_id") or "").strip() or None
        if order_id and order_id != existing_order_id:
            raise HTTPException(status_code=409, detail="document_id is not linked to the requested trade order.")
        order_id = existing_order_id
    order = None
    items = []
    if order_id:
        orders = (
            db.table("trade_orders").select("*").eq("id", order_id)
            .eq("organization_id", org_id).limit(1).execute()
        ).data or []
        if not orders:
            raise HTTPException(status_code=404, detail="Trade order not found in this organization.")
        order = orders[0]
        items = (
            db.table("trade_order_items").select("*").eq("order_id", order_id)
            .eq("organization_id", org_id).execute()
        ).data or []
    generated, model = _ai_json(
        "You draft business trade documents. Return JSON with keys: title (string), body (string), "
        "structured_data (object), assumptions (array of strings), missing_information (array of strings). "
        "Use only supplied facts; never invent legal terms, parties, prices, dates, HS classifications, "
        "certifications, or customs declarations. Mark unknowns as placeholders such as [REQUIRED]. "
        "Write neutral, professional content. This is a draft for human review, not legal or customs advice.",
        {"document_type": doc_type, "requested_title": title, "instructions": instructions, "trade_order": order, "items": items},
    )
    body = str(generated.get("body") or "").strip()
    if not body:
        raise HTTPException(status_code=502, detail="AI returned an empty document draft.")
    structured = generated.get("structured_data") if isinstance(generated.get("structured_data"), dict) else {}
    structured.update({
        "assumptions": generated.get("assumptions") if isinstance(generated.get("assumptions"), list) else [],
        "missing_information": generated.get("missing_information") if isinstance(generated.get("missing_information"), list) else [],
        "ai_model": model,
        "ai_generated": True,
    })
    try:
        draft_values = {
            "title": str(generated.get("title") or title)[:200],
            "status": "DRAFT",
            "extracted_text": body,
            "structured_data": {**structured, **(target_document.get("structured_data") or {})} if target_document else structured,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if target_document:
            row = db.table("trade_documents").update(draft_values).eq(
                "id", target_document_id
            ).eq("organization_id", org_id).execute().data
        else:
            row = db.table("trade_documents").insert({
                "organization_id": org_id,
                "order_id": order_id,
                "document_type": doc_type,
                **draft_values,
                "created_by": user_id,
            }).execute().data
        if not row:
            raise RuntimeError("Document draft persistence returned no row.")
        return {"success": True, "document": row[0], "review_required": True}
    except Exception:
        logger.exception("Trade document draft persistence failed.")
        raise HTTPException(status_code=503, detail="Document draft could not be saved.")


@router.post("/{document_id}/review", status_code=201)
def review_trade_document(
    document_id: str,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default=""),
):
    db, _, org_id = _auth_context(authorization, x_organization_id)
    document = _document(db, document_id, org_id)
    if document.get("status") == "APPROVED":
        raise HTTPException(status_code=409, detail="Approved documents must be amended and returned to DRAFT before another review.")
    content = str(document.get("extracted_text") or "")
    if not content.strip():
        raise HTTPException(status_code=422, detail="Document has no text to review.")
    result, model = _ai_json(
        "You perform a cautious first-pass commercial document risk review. Return JSON keys: "
        "overall_risk (LOW|MEDIUM|HIGH|CRITICAL), findings (array), missing_clauses (array), "
        "payment_term_findings (array), compliance_findings (array), summary (string). "
        "Each finding should include severity, category, detail, and suggested_action. "
        "Do not claim legal compliance, tariff correctness, or regulatory approval. Distinguish "
        "confirmed omissions from items that require human verification. This is decision support only.",
        {"document_type": document.get("document_type"), "title": document.get("title"), "document_text": content[:30000]},
    )
    risk = str(result.get("overall_risk") or "MEDIUM").upper()
    if risk not in RISK_LEVELS:
        risk = "MEDIUM"
    def arr(key):
        value = result.get(key)
        return value if isinstance(value, list) else []
    try:
        review = db.table("trade_document_reviews").insert({
            "organization_id": org_id,
            "document_id": document_id,
            "review_status": "COMPLETED",
            "overall_risk": risk,
            "findings": arr("findings"),
            "missing_clauses": arr("missing_clauses"),
            "payment_term_findings": arr("payment_term_findings"),
            "compliance_findings": arr("compliance_findings"),
            "model_version": model,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }).execute().data
        db.table("trade_documents").update({"status": "PENDING_REVIEW"}).eq(
            "id", document_id
        ).eq("organization_id", org_id).execute()
        if not review:
            raise RuntimeError("Review insert returned no row.")
        return {"success": True, "review": review[0], "summary": str(result.get("summary") or "")}
    except Exception:
        logger.exception("Trade document review persistence failed.")
        raise HTTPException(status_code=503, detail="Document review could not be saved.")


@router.post("/{document_id}/decision")
def decide_trade_document(
    document_id: str,
    payload: dict,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default=""),
):
    db, user_id, org_id = _auth_context(authorization, x_organization_id)
    document = _document(db, document_id, org_id)
    if document.get("status") != "PENDING_REVIEW":
        raise HTTPException(status_code=409, detail="A human decision is allowed only while the document is PENDING_REVIEW.")
    decision = str((payload or {}).get("decision") or "").strip().upper()
    if decision not in {"APPROVED", "REJECTED"}:
        raise HTTPException(status_code=422, detail="decision must be APPROVED or REJECTED.")
    reviews = (
        db.table("trade_document_reviews").select("overall_risk,review_status")
        .eq("document_id", document_id).eq("organization_id", org_id)
        .order("created_at", desc=True).limit(1).execute()
    ).data or []
    if not reviews or reviews[0].get("review_status") != "COMPLETED":
        raise HTTPException(status_code=409, detail="A completed AI review is required before a human decision.")
    risk = str(reviews[0].get("overall_risk") or "UNASSESSED").upper()
    if decision == "APPROVED" and risk in {"HIGH", "CRITICAL"} and (payload or {}).get("acknowledge_high_risk") is not True:
        raise HTTPException(status_code=409, detail="Explicit high-risk acknowledgement is required before approval.")
    update = {
        "status": decision,
        "approved_by": user_id if decision == "APPROVED" else None,
        "approved_at": datetime.now(timezone.utc).isoformat() if decision == "APPROVED" else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        rows = db.table("trade_documents").update(update).eq(
            "id", document_id
        ).eq("organization_id", org_id).eq(
            "status", "PENDING_REVIEW"
        ).execute().data
        if not rows:
            raise HTTPException(
                status_code=409,
                detail="The document review state changed. Reload the document before deciding.",
            )
        return {"success": True, "document": rows[0], "review_risk": risk}
    except Exception:
        logger.exception("Trade document human decision failed.")
        raise HTTPException(status_code=503, detail="Document decision could not be saved.")


@router.post("/{document_id}/render")
def render_trade_document(
    document_id: str,
    payload: dict,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default=""),
):
    db, _, org_id = _auth_context(authorization, x_organization_id)
    document = _document(db, document_id, org_id)
    fmt = str((payload or {}).get("format") or "pdf").lower()
    if fmt not in {"pdf", "docx"}:
        raise HTTPException(status_code=422, detail="format must be pdf or docx.")
    content = str(document.get("extracted_text") or "").strip()
    if not content:
        raise HTTPException(status_code=422, detail="Document has no content to render.")
    try:
        output = io.BytesIO()
        if fmt == "pdf":
            from reportlab.lib.pagesizes import letter
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.lib.units import inch
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
            from xml.sax.saxutils import escape
            doc = SimpleDocTemplate(output, pagesize=letter, rightMargin=0.7*inch, leftMargin=0.7*inch)
            styles = getSampleStyleSheet()
            story = [Paragraph(escape(str(document.get("title") or "Trade Document")), styles["Title"]), Spacer(1, 12)]
            for paragraph in content.splitlines():
                if paragraph.strip():
                    story.append(Paragraph(escape(paragraph), styles["BodyText"]))
                    story.append(Spacer(1, 6))
            doc.build(story)
            mime = "application/pdf"
            extension = "pdf"
        else:
            from docx import Document
            doc = Document()
            doc.add_heading(str(document.get("title") or "Trade Document"), 0)
            for paragraph in content.splitlines():
                if paragraph.strip():
                    doc.add_paragraph(paragraph)
            doc.save(output)
            mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            extension = "docx"
        data = output.getvalue()
        if not data or len(data) > MAX_FILE_BYTES:
            raise ValueError("Generated document size is invalid.")
        version = int(document.get("version") or 1)
        filename = _safe_filename(str(document.get("title") or "trade-document"), extension)
        path = f"{org_id}/{document_id}/v{version}/{uuid.uuid4().hex}-{filename}"
        db.storage.from_(BUCKET).upload(path, data, {"content-type": mime, "upsert": "true"})
        extracted_text = None
        try:
            if mime == "text/plain":
                extracted_text = data.decode("utf-8", errors="replace")[:100000]
            elif mime == "application/pdf":
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(data))
                extracted_text = "\n".join(page.extract_text() or "" for page in reader.pages)[:100000]
            elif mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                from docx import Document
                parsed = Document(io.BytesIO(data))
                extracted_text = "\n".join(p.text for p in parsed.paragraphs)[:100000]
        except Exception:
            logger.exception("Text extraction failed for uploaded trade document.")
            extracted_text = None
        updated = db.table("trade_documents").update({
            "storage_path": path,
            "mime_type": mime,
            "extracted_text": extracted_text,
            "status": "DRAFT",
            "approved_by": None,
            "approved_at": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", document_id).eq("organization_id", org_id).execute().data
        if not updated:
            raise RuntimeError("Rendered file was uploaded but document metadata was not updated.")
        return {"success": True, "document_id": document_id, "storage_path": path, "mime_type": mime, "format": fmt}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Trade document rendering/upload failed.")
        raise HTTPException(status_code=503, detail="Document could not be rendered or stored.")


@router.post("/{document_id}/upload")
async def upload_trade_document(
    document_id: str,
    request: Request,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default=""),
):
    db, _, org_id = _auth_context(authorization, x_organization_id)
    document = _document(db, document_id, org_id)
    mime = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if mime not in ALLOWED_MIME_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported document content type.")
    declared_size = request.headers.get("content-length")
    if declared_size:
        try:
            if int(declared_size) > MAX_FILE_BYTES:
                raise HTTPException(status_code=413, detail="Upload exceeds the 25 MB limit.")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header.")
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail="Upload exceeds the 25 MB limit.")
    data = bytes(chunks)
    if not data:
        raise HTTPException(status_code=400, detail="An upload body is required.")
    if not _valid_file_signature(mime, data):
        raise HTTPException(status_code=415, detail="File signature does not match the declared content type.")
    raw_name = request.query_params.get("filename", "uploaded-document")
    filename = PurePosixPath(raw_name.replace("\\", "/")).name
    filename = _safe_filename(filename.rsplit(".", 1)[0], filename.rsplit(".", 1)[-1] if "." in filename else "bin")
    version = int(document.get("version") or 1)
    path = f"{org_id}/{document_id}/v{version}/{uuid.uuid4().hex}-{filename}"
    try:
        db.storage.from_(BUCKET).upload(path, data, {"content-type": mime, "upsert": "false"})
        extracted_text = None
        try:
            if mime == "text/plain":
                extracted_text = data.decode("utf-8", errors="replace")[:100000]
            elif mime == "application/pdf":
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(data))
                extracted_text = "\n".join(page.extract_text() or "" for page in reader.pages)[:100000]
            elif mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                from docx import Document
                parsed = Document(io.BytesIO(data))
                extracted_text = "\n".join(p.text for p in parsed.paragraphs)[:100000]
        except Exception:
            logger.exception("Text extraction failed for uploaded trade document.")
        updated = db.table("trade_documents").update({
            "storage_path": path,
            "mime_type": mime,
            "extracted_text": extracted_text,
            "status": "DRAFT",
            "approved_by": None,
            "approved_at": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", document_id).eq("organization_id", org_id).execute().data
        if not updated:
            raise RuntimeError("Upload metadata update returned no row.")
        return {"success": True, "document_id": document_id, "storage_path": path, "mime_type": mime, "bytes": len(data)}
    except Exception:
        logger.exception("Trade document upload failed.")
        raise HTTPException(status_code=503, detail="Document upload could not be completed.")


@router.get("/{document_id}/download")
def get_trade_document_download(
    document_id: str,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default=""),
):
    db, _, org_id = _auth_context(authorization, x_organization_id)
    document = _document(db, document_id, org_id)
    path = str(document.get("storage_path") or "")
    if not path or not path.startswith(f"{org_id}/{document_id}/"):
        raise HTTPException(status_code=404, detail="Stored document not found.")
    try:
        signed = db.storage.from_(BUCKET).create_signed_url(path, 600)
        url = signed.get("signedURL") or signed.get("signedUrl")
        if not url:
            raise RuntimeError("Storage did not return a signed URL.")
        return {"success": True, "signed_url": url, "expires_in": 600, "mime_type": document.get("mime_type")}
    except Exception:
        logger.exception("Trade document signed URL creation failed.")
        raise HTTPException(status_code=503, detail="Secure document download is unavailable.")


def _usitc_search_records(payload):
    """Normalize the documented USITC search response without assuming one wrapper shape."""
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = None
        for key in ("results", "data", "items", "HTSDetails", "htsDetails"):
            if isinstance(payload.get(key), list):
                records = payload[key]
                break
        if records is None:
            records = [payload] if payload.get("htsno") else []
    else:
        records = []
    normalized = []
    for item in records[:100]:
        if not isinstance(item, dict):
            continue
        code = item.get("htsno") or item.get("hts_number") or item.get("htsNo")
        description = item.get("description") or item.get("desc")
        if not code and not description:
            continue
        normalized.append({
            "hts_number": str(code) if code is not None else None,
            "description": str(description) if description is not None else None,
            "general_rate": item.get("general"),
            "special_rate": item.get("special"),
            "column_2_rate": item.get("other"),
            "units": item.get("units"),
            "indent": item.get("indent"),
            "footnotes": item.get("footnotes"),
            "publication_id": item.get("publication_id") or item.get("publicationId"),
        })
    return normalized


@router.post("/customs/hs-code/lookup")
def lookup_us_customs_hs_code(
    payload: dict,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default=""),
):
    _db, _, _org_id = _auth_context(authorization, x_organization_id)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="A JSON object is required.")

    import_country = str(payload.get("import_country") or "").strip()
    if import_country.casefold() not in {"us", "usa", "u.s.", "united states", "united states of america"}:
        raise HTTPException(
            status_code=422,
            detail="The connected official schedule is the United States HTSUS only. Non-U.S. import markets are not supported by this lookup.",
        )

    raw_code = str(payload.get("hs_code") or "").strip()
    description = str(payload.get("product_description") or "").strip()
    if raw_code:
        code_check = _hs_code_format_check(raw_code)
        if not code_check["valid_format"]:
            raise HTTPException(status_code=422, detail=code_check["message"])
        keyword = code_check["normalized_code"]
        search_mode = "hts_code_or_schedule_text"
    else:
        if not description:
            raise HTTPException(status_code=422, detail="Provide hs_code or product_description.")
        if len(description) > 120:
            raise HTTPException(status_code=422, detail="product_description must be 120 characters or fewer.")
        keyword = description
        search_mode = "product_description_keyword"

    source_url = "https://hts.usitc.gov/reststop/search"
    try:
        import httpx
        response = httpx.get(
            source_url,
            params={"keyword": keyword},
            timeout=httpx.Timeout(8.0, connect=3.0),
            headers={"Accept": "application/json", "User-Agent": "AGS-Pulse-Trade-Documents/1.0"},
            follow_redirects=True,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").casefold()
        body_prefix = response.text.lstrip()[:1]
        if "json" not in content_type and body_prefix not in {"[", "{"}:
            raise ValueError("USITC returned a non-JSON response.")
        data = response.json()
        records = _usitc_search_records(data)
    except Exception as exc:
        logger.warning("USITC HTS lookup unavailable: %s", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail="The official USITC HTS lookup is temporarily unavailable or returned an unexpected response. No classification was verified.",
        )

    normalized_code = re.sub(r"[ .-]", "", raw_code)
    if normalized_code:
        exact = [
            row for row in records
            if re.sub(r"[^0-9]", "", str(row.get("hts_number") or "")) == normalized_code
        ]
        records = exact or records

    return {
        "success": True,
        "market": "United States",
        "schedule": "Harmonized Tariff Schedule of the United States (HTSUS)",
        "search_mode": search_mode,
        "query": keyword,
        "result_count": len(records),
        "results": records[:50],
        "source": "United States International Trade Commission (USITC)",
        "source_url": source_url,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "classification_status": "CANDIDATES_RETRIEVED_NOT_CLASSIFIED",
        "tariff_rates_status": "SOURCE_DATA_RETRIEVED_ADVISORY",
        "human_customs_review_required": True,
        "important_notice": "USITC search results are candidate schedule entries, not a legally binding classification or complete duty calculation. Search results may omit related provisions, Chapter 99 measures, trade remedies, origin rules, and current CBP rulings. Confirm the current HTSUS and obtain qualified customs review before filing.",
    }

# USITC lookup implementation is intentionally limited to the U.S. HTSUS.
