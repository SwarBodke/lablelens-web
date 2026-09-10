"""LableLens FastAPI backend.

Features:
- hybrid RapidOCR + Tesseract OCR with real confidence reporting
- six grouped LMPC screening checks with tri-state outcomes
- multi-panel physical package analysis and e-commerce listing text analysis
- 24h signed guest sessions and optional explicit demo-reviewer mode
- session-scoped inspection history
- SHA-256 evidence preservation
- real PDF, DOCX and JSON report downloads
"""
from __future__ import annotations

import json
import os
import importlib.util
from pathlib import Path

import cv2
import numpy as np
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from compliance_engine import ComplianceEngine
from database import (
    get_catalog,
    get_inspection_by_id,
    get_inspections,
    lookup_product,
    register_or_update_product,
    save_inspection,
    session_stats,
    purge_expired_guest_data,
    delete_session_data,
    verify_session_chain,
)
from evidence import preserve_image, remove_session_evidence
from reports import build_docx, build_pdf, report_payload
from ruleset import metadata as ruleset_metadata
from inspection_rule_contract import finalize_six_group_result
from session_auth import (
    issue_session, reviewer_login, staff_login, verify_token, google_staff_login, google_auth_public_config,
    GoogleAuthConfigurationError, GoogleCredentialError, GoogleAccessDenied, GOOGLE_AUTH_ENABLED,
    ALLOW_DEMO_REVIEWER, ALLOW_DEMO_ADMIN, ALLOW_DEMO_OFFICER,
)
from management import (
    admin_overview, analytics as admin_analytics, audit_event, create_case,
    get_case, get_inspection_for_actor, list_all_inspections, list_audit,
    list_cases, list_officers, officer_detail, officer_update_case, register_actor, update_case,
)

BASE_DIR = Path(__file__).resolve().parent
MAX_FILE_SIZE = 15 * 1024 * 1024
MAX_IMAGES = 4
MAX_PIXELS = max(1_000_000, int(os.getenv("MAX_IMAGE_PIXELS", "30000000")))
GUEST_SCAN_LIMIT = max(1, int(os.getenv("GUEST_SCAN_LIMIT", "10")))
ALLOWED_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/bmp", "image/tiff", "application/octet-stream"}

app = FastAPI(
    title="LableLens Legal Metrology Screening API",
    description="AI-assisted Legal Metrology compliance screening for packaged commodities",
    version="2.7.0",
)

origins = [x.strip() for x in os.getenv("ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

_engine: Optional[ComplianceEngine] = None


def engine() -> ComplianceEngine:
    global _engine
    if _engine is None:
        _engine = ComplianceEngine()
    return _engine


def purge_expired_workspaces() -> int:
    removed = purge_expired_guest_data()
    for sid in removed:
        remove_session_evidence(sid)
    return len(removed)


@app.on_event("startup")
def startup():
    # Initialise the light wrapper; RapidOCR itself stays lazy to keep startup resilient.
    try:
        removed = purge_expired_workspaces()
        if removed:
            print(f"[Startup] Purged {removed} expired guest workspace(s)")
        e = engine()
        print(f"[Startup] OCR ready. Tesseract languages: {e.ocr.languages}; executable: {e.ocr.tesseract_cmd or 'PATH lookup'}")
    except Exception as exc:
        print(f"[Startup] initialisation warning: {exc}")


class TextAnalysisRequest(BaseModel):
    text: str = Field(min_length=3, max_length=100_000)
    product_name: Optional[str] = Field(default=None, max_length=255)
    evidence_scope: str = "complete_listing"
    imported: Optional[bool] = None
    category: str = Field(default="general", max_length=50)


class CatalogConfirmRequest(BaseModel):
    product_name: str = Field(min_length=2, max_length=255)
    company: Optional[str] = Field(default=None, max_length=500)
    quantity: Optional[str] = Field(default=None, max_length=100)
    fssai: Optional[str] = Field(default=None, max_length=50)
    mrp: Optional[float] = None


class ReviewerLoginRequest(BaseModel):
    code: str = Field(min_length=1, max_length=200)


class StaffLoginRequest(BaseModel):
    role: str = Field(pattern="^(admin|officer)$")
    code: str = Field(min_length=1, max_length=200)


class GoogleLoginRequest(BaseModel):
    credential: str = Field(min_length=100, max_length=12000)


class CaseCreateRequest(BaseModel):
    remarks: str = Field(default="", max_length=4000)
    priority: str = Field(default="normal", pattern="^(low|normal|high|urgent)$")
    submit: bool = False
    case_type: str = Field(default="compliance_case", pattern="^(compliance_case|manual_review)$")


class CaseUpdateRequest(BaseModel):
    status: str = Field(pattern="^(draft|submitted|under_review|action_required|resolved|rejected|closed)$")
    admin_notes: str = Field(default="", max_length=4000)
    priority: Optional[str] = Field(default=None, pattern="^(low|normal|high|urgent)$")


class CaseOfficerUpdateRequest(BaseModel):
    status: str = Field(pattern="^(submitted|closed)$")
    remarks: str = Field(default="", max_length=4000)


def require_session(authorization: Optional[str]) -> Dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="A valid LableLens session is required")
    payload = verify_token(authorization.split(" ", 1)[1].strip())
    if not payload:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    return payload


def session_from_header(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    return require_session(authorization)


def require_role(session: Dict[str, Any], *roles: str) -> Dict[str, Any]:
    if session.get("role") not in roles:
        raise HTTPException(status_code=403, detail="This action is not permitted for the current role")
    return session


def enforce_scan_limit(session: Dict[str, Any]) -> None:
    if session.get("role") != "guest":
        return
    used = session_stats(session["sid"]).get("total", 0)
    if used >= GUEST_SCAN_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Guest workspace scan limit reached ({GUEST_SCAN_LIMIT}). Start a new guest workspace after this one expires or use an authorised deployment account.",
        )


def parse_context(
    evidence_scope: str = "partial",
    imported: Optional[str] = None,
    category: str = "general",
    font_height_mm: Optional[str] = None,
    font_width_mm: Optional[str] = None,
    pdp_area_cm2: Optional[str] = None,
    moulded_text: Optional[str] = None,
) -> Dict[str, Any]:
    allowed_categories = {"general", "food", "cosmetics", "medical_device", "electronics", "other"}
    category = (category or "general").strip().lower()
    ctx: Dict[str, Any] = {"evidence_scope": evidence_scope, "category": category if category in allowed_categories else "other"}
    if imported is not None:
        ctx["imported"] = str(imported).lower() in {"1", "true", "yes", "on"}
    if font_height_mm not in (None, ""): ctx["font_height_mm"] = font_height_mm
    if font_width_mm not in (None, ""): ctx["font_width_mm"] = font_width_mm
    if pdp_area_cm2 not in (None, ""): ctx["pdp_area_cm2"] = pdp_area_cm2
    if moulded_text is not None: ctx["moulded_text"] = str(moulded_text).lower() in {"1", "true", "yes", "on"}
    return ctx


def resolve_product_name(result: Dict[str, Any], user_name: Optional[str], session_id: Optional[str] = None) -> str:
    if user_name and user_name.strip():
        result["product_name_source"] = "user_confirmed_input"
        return user_name.strip()[:255]
    detected = (result.get("product_name") or "").strip()
    if detected:
        result["product_name_source"] = "ocr_candidate"
        return detected[:255]
    learned = lookup_product(
        fssai=result.get("fssai"), company=result.get("company"), quantity=result.get("quantity"),
        raw_text=result.get("raw_text"), session_id=session_id,
    )
    if learned:
        result["product_name_source"] = "confirmed_catalog"
        return learned[:255]
    result["product_name_source"] = "unknown"
    return "Unnamed product"


async def read_upload(upload: UploadFile) -> bytes:
    ctype = (upload.content_type or "application/octet-stream").lower()
    if ctype not in ALLOWED_TYPES and not ctype.startswith("image/"):
        raise HTTPException(status_code=415, detail=f"Unsupported image type: {ctype}")
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded image is empty")
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail=f"Image exceeds {MAX_FILE_SIZE // (1024*1024)} MB limit")
    # Decode once before OCR so malformed files and decompression-bomb-sized images
    # fail cleanly instead of reaching the OCR workers.
    arr = np.frombuffer(data, dtype=np.uint8)
    decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if decoded is None:
        raise HTTPException(status_code=415, detail="Uploaded file is not a decodable image")
    height, width = decoded.shape[:2]
    if int(height) * int(width) > MAX_PIXELS:
        raise HTTPException(status_code=413, detail=f"Image dimensions are too large; maximum decoded area is {MAX_PIXELS:,} pixels")
    return data


@app.get("/health")
def health():
    e = engine()
    return {
        "status": "ok",
        "service": "LableLens",
        "version": app.version,
        "tesseract_languages": e.ocr.languages,
        "tesseract_available": bool(e.ocr.tesseract_cmd),
        "onnxruntime_available": importlib.util.find_spec("onnxruntime") is not None,
        "rapidocr_available": importlib.util.find_spec("rapidocr") is not None,
        "ocr_mode": e.ocr.mode,
        "tesseract_fast_confidence_threshold": e.ocr.tesseract_fast_confidence_threshold,
        "tesseract_fast_coverage_threshold": e.ocr.tesseract_fast_coverage_threshold,
        "tesseract_min_chars": e.ocr.tesseract_min_chars,
        "accuracy_guard": e.ocr.accuracy_guard,
        "rapid_confidence_threshold": e.ocr.rapid_confidence_threshold,
        "rapid_coverage_threshold": e.ocr.rapid_coverage_threshold,
        "tesseract_timeout_sec": e.ocr.tesseract_timeout,
        "demo_reviewer_enabled": ALLOW_DEMO_REVIEWER,
        "officer_access_enabled": ALLOW_DEMO_OFFICER,
        "admin_access_enabled": ALLOW_DEMO_ADMIN,
        "google_staff_auth_enabled": GOOGLE_AUTH_ENABLED,
        "guest_scan_limit": GUEST_SCAN_LIMIT,
        "ruleset": ruleset_metadata().get("id"),
    }


@app.get("/api/ruleset")
def ruleset():
    return ruleset_metadata()


@app.get("/api/auth/config")
def auth_config():
    """Public, non-secret identity-provider configuration for the login UI."""
    return google_auth_public_config()


@app.post("/api/session/guest")
def create_guest_session():
    purge_expired_workspaces()
    session = issue_session(role="guest", label="Guest")
    session["scan_limit"] = GUEST_SCAN_LIMIT
    return session


@app.post("/api/session/reviewer")
def create_reviewer_session(req: ReviewerLoginRequest):
    result = reviewer_login(req.code)
    if not result:
        raise HTTPException(status_code=403, detail="Officer access is disabled or the code is invalid")
    register_actor(result)
    audit_event(result, "officer_login", "session", result.get("sid"))
    return result


@app.post("/api/session/staff")
def create_staff_session(req: StaffLoginRequest):
    result = staff_login(req.role, req.code)
    if not result:
        raise HTTPException(status_code=403, detail=f"{req.role.title()} access is disabled or the code is invalid")
    register_actor(result)
    audit_event(result, f"{req.role}_login", "session", result.get("sid"))
    return result


@app.post("/api/session/google")
def create_google_staff_session(req: GoogleLoginRequest):
    try:
        result = google_staff_login(req.credential)
    except GoogleAuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GoogleCredentialError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except GoogleAccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    register_actor(result)
    audit_event(
        result, f"{result.get('role')}_login", "session", result.get("sid"),
        {"provider": "google", "email": result.get("email")},
    )
    return result


@app.delete("/api/session")
def delete_current_session(authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization)
    deleted = delete_session_data(session["sid"])
    remove_session_evidence(session["sid"])
    return {"ok": True, "deleted_inspections": deleted}


@app.post("/api/analyze")
async def analyze_single(
    image: UploadFile = File(...),
    product_name: Optional[str] = Form(default=None),
    evidence_scope: str = Form(default="partial"),
    category: str = Form(default="general"),
    imported: Optional[str] = Form(default=None),
    font_height_mm: Optional[str] = Form(default=None),
    font_width_mm: Optional[str] = Form(default=None),
    pdp_area_cm2: Optional[str] = Form(default=None),
    moulded_text: Optional[str] = Form(default=None),
    authorization: Optional[str] = Header(default=None),
):
    session = require_session(authorization)
    enforce_scan_limit(session)
    data = await read_upload(image)
    ctx = parse_context(evidence_scope=evidence_scope, imported=imported, category=category, font_height_mm=font_height_mm, font_width_mm=font_width_mm, pdp_area_cm2=pdp_area_cm2, moulded_text=moulded_text)
    result = finalize_six_group_result(engine().analyze_images([data], context=ctx))
    final_name = resolve_product_name(result, product_name, session["sid"])
    result["product_name"] = final_name
    evidence = [preserve_image(data, session["sid"], image.content_type, 0)]
    result["evidence"] = [{k: v for k, v in ev.items() if k != "path"} for ev in evidence]
    inspection_id = save_inspection(result, session=session, product_name=final_name, evidence=evidence)
    result["inspection_id"] = inspection_id
    audit_event(session, "inspection_created", "inspection", inspection_id, {"source_type": result.get("source_type")})
    audit_event(session, "inspection_completed", "inspection", inspection_id, {"status": result.get("compliance_status"), "source_type": result.get("source_type")})
    return result


@app.post("/api/analyze/multi")
async def analyze_multi(
    images: List[UploadFile] = File(...),
    product_name: Optional[str] = Form(default=None),
    evidence_scope: str = Form(default="partial"),
    category: str = Form(default="general"),
    imported: Optional[str] = Form(default=None),
    font_height_mm: Optional[str] = Form(default=None),
    font_width_mm: Optional[str] = Form(default=None),
    pdp_area_cm2: Optional[str] = Form(default=None),
    moulded_text: Optional[str] = Form(default=None),
    authorization: Optional[str] = Header(default=None),
):
    session = require_session(authorization)
    enforce_scan_limit(session)
    if not (1 <= len(images) <= MAX_IMAGES):
        raise HTTPException(status_code=400, detail=f"Upload between 1 and {MAX_IMAGES} package panels")
    blobs, evidence = [], []
    for i, upload in enumerate(images):
        b = await read_upload(upload)
        blobs.append(b)
        evidence.append(preserve_image(b, session["sid"], upload.content_type, i))
    ctx = parse_context(evidence_scope=evidence_scope, imported=imported, category=category, font_height_mm=font_height_mm, font_width_mm=font_width_mm, pdp_area_cm2=pdp_area_cm2, moulded_text=moulded_text)
    result = finalize_six_group_result(engine().analyze_images(blobs, context=ctx))
    final_name = resolve_product_name(result, product_name, session["sid"])
    result["product_name"] = final_name
    result["evidence"] = [{k: v for k, v in ev.items() if k != "path"} for ev in evidence]
    inspection_id = save_inspection(result, session=session, product_name=final_name, evidence=evidence)
    result["inspection_id"] = inspection_id
    audit_event(session, "inspection_created", "inspection", inspection_id, {"source_type": result.get("source_type")})
    audit_event(session, "inspection_completed", "inspection", inspection_id, {"status": result.get("compliance_status"), "source_type": result.get("source_type")})
    return result


@app.post("/api/analyze/text")
def analyze_listing(req: TextAnalysisRequest, authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization)
    enforce_scan_limit(session)
    ctx = {"source_type": "ecommerce", "evidence_scope": req.evidence_scope, "imported": req.imported, "category": req.category}
    result = finalize_six_group_result(engine().analyze_text(req.text, context=ctx))
    final_name = resolve_product_name(result, req.product_name, session["sid"])
    result["product_name"] = final_name
    inspection_id = save_inspection(result, session=session, product_name=final_name, evidence=[])
    result["inspection_id"] = inspection_id
    audit_event(session, "inspection_created", "inspection", inspection_id, {"source_type": result.get("source_type")})
    audit_event(session, "inspection_completed", "inspection", inspection_id, {"status": result.get("compliance_status"), "source_type": result.get("source_type")})
    return result


@app.post("/api/analyze/ecommerce")
async def analyze_ecommerce_evidence(
    images: List[UploadFile] = File(default=[]),
    listing_text: str = Form(default=""),
    product_name: Optional[str] = Form(default=None),
    evidence_scope: str = Form(default="partial"),
    category: str = Form(default="general"),
    imported: Optional[str] = Form(default=None),
    authorization: Optional[str] = Header(default=None),
):
    """Analyze e-commerce screenshots, pasted listing text, or both.

    The text and OCR transcription are merged before the same Rule 6 screening
    logic is applied. Manufacture/packing month-year remains excluded from the
    e-commerce network display check as handled by the compliance engine.
    """
    session = require_session(authorization)
    enforce_scan_limit(session)
    if len(images) > MAX_IMAGES:
        raise HTTPException(status_code=400, detail=f"Upload at most {MAX_IMAGES} listing screenshots")
    if not images and len((listing_text or "").strip()) < 3:
        raise HTTPException(status_code=400, detail="Provide listing text, at least one screenshot, or both")
    blobs, evidence = [], []
    for i, upload in enumerate(images):
        b = await read_upload(upload)
        blobs.append(b)
        evidence.append(preserve_image(b, session["sid"], upload.content_type, i))
    ctx = parse_context(evidence_scope=evidence_scope, imported=imported, category=category)
    ctx["source_type"] = "ecommerce"
    if blobs:
        result = engine().analyze_images(blobs, context=ctx)
        if (listing_text or "").strip():
            merged_text = result.get("raw_text", "") + "\n" + listing_text.strip()
            text_result = engine().analyze_text(merged_text, context=ctx)
            # Keep image OCR confidence/codes while using the merged-text legal result.
            text_result["ocr_confidence"] = result.get("ocr_confidence")
            text_result["confidence"] = result.get("confidence")
            text_result["ocr_confidence_by_panel"] = result.get("ocr_confidence_by_panel")
            text_result["engines_used"] = result.get("engines_used")
            text_result["codes"] = result.get("codes") or []
            result = text_result
    else:
        result = engine().analyze_text(listing_text.strip(), context=ctx)
    result = finalize_six_group_result(result)
    final_name = resolve_product_name(result, product_name, session["sid"])
    result["product_name"] = final_name
    result["evidence"] = [{k: v for k, v in ev.items() if k != "path"} for ev in evidence]
    inspection_id = save_inspection(result, session=session, product_name=final_name, evidence=evidence)
    result["inspection_id"] = inspection_id
    audit_event(session, "inspection_created", "inspection", inspection_id, {"source_type": result.get("source_type")})
    audit_event(session, "inspection_completed", "inspection", inspection_id, {"status": result.get("compliance_status"), "source_type": result.get("source_type")})
    return result


@app.get("/api/inspections")
def list_inspections(limit: int = 50, authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization)
    if session.get("role") == "admin":
        return list_all_inspections(limit=limit)
    if session.get("role") == "officer":
        return list_all_inspections(limit=limit, actor_id=session.get("actor_id"))
    return get_inspections(session["sid"], limit=limit)


@app.get("/api/inspections/stats")
def inspection_stats(authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization)
    if session.get("role") == "guest":
        stats = session_stats(session["sid"])
        stats["audit_chain"] = verify_session_chain(session["sid"])
        stats["scan_limit"] = GUEST_SCAN_LIMIT
        stats["scans_remaining"] = max(0, GUEST_SCAN_LIMIT - stats.get("total", 0))
        return stats
    rows = list_all_inspections(limit=1000, actor_id=session.get("actor_id")) if session.get("role") == "officer" else list_all_inspections(limit=1000)
    counts = {"compliant": 0, "non_compliant": 0, "needs_review": 0}
    for row in rows:
        st = row.get("compliance_status") or "needs_review"; counts[st] = counts.get(st, 0) + 1
    resolved = counts.get("compliant",0)+counts.get("non_compliant",0)
    return {"total":len(rows), **counts, "compliance_rate": round(100*counts.get("compliant",0)/resolved) if resolved else None, "scan_limit": None, "scans_remaining": None}


@app.get("/api/inspections/{inspection_id}")
def inspection(inspection_id: int, authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization)
    record = get_inspection_for_actor(inspection_id, session)
    if not record:
        raise HTTPException(status_code=404, detail="Inspection not found or not accessible")
    if isinstance(record.get("evidence"), list):
        record["evidence"] = [{k: v for k, v in x.items() if k != "path"} for x in record["evidence"]]
    return record


@app.get("/api/inspections/{inspection_id}/evidence/{index}")
def inspection_evidence(inspection_id: int, index: int, authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization)
    record = get_inspection_for_actor(inspection_id, session)
    if not record:
        raise HTTPException(status_code=404, detail="Inspection not found or not accessible")
    evidence = record.get("evidence") or []
    if not isinstance(evidence, list) or index < 0 or index >= len(evidence):
        raise HTTPException(status_code=404, detail="Evidence image not found")
    path = evidence[index].get("path")
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Original evidence is not preserved on this deployment")
    return FileResponse(path, media_type=evidence[index].get("content_type") or "application/octet-stream")


@app.get("/api/inspections/{inspection_id}/report.{format}")
def report(inspection_id: int, format: str, authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization)
    record = get_inspection_for_actor(inspection_id, session)
    if not record:
        raise HTTPException(status_code=404, detail="Inspection not found or not accessible")
    safe_record = dict(record)
    if isinstance(safe_record.get("evidence"), list):
        safe_record["evidence"] = [{k: v for k, v in x.items() if k != "path"} for x in safe_record["evidence"]]
    fmt = format.lower()
    audit_event(session, "report_generated", "inspection", inspection_id, {"format": fmt})
    if fmt == "json":
        return JSONResponse(report_payload(safe_record), headers={"Content-Disposition": f'attachment; filename="LableLens-INS-{inspection_id}.json"'})
    if fmt == "pdf":
        return Response(build_pdf(safe_record), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="LableLens-INS-{inspection_id}.pdf"'})
    if fmt in {"docx", "word"}:
        return Response(build_docx(safe_record), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers={"Content-Disposition": f'attachment; filename="LableLens-INS-{inspection_id}.docx"'})
    raise HTTPException(status_code=400, detail="Supported report formats: pdf, docx, json")


@app.post("/api/inspections/{inspection_id}/complaints")
def file_case(inspection_id: int, req: CaseCreateRequest, authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization); require_role(session, "officer", "admin")
    try:
        return create_case(inspection_id, session, remarks=req.remarks, priority=req.priority, submit=req.submit, case_type=req.case_type)
    except PermissionError as exc: raise HTTPException(status_code=403, detail=str(exc))
    except LookupError as exc: raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc: raise HTTPException(status_code=409, detail=str(exc))


@app.get("/api/cases")
def my_cases(limit: int = 100, authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization); require_role(session, "officer", "admin")
    return list_cases(session, limit=limit)


@app.get("/api/cases/{case_id}")
def case_detail(case_id: int, authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization); require_role(session, "officer", "admin")
    case = get_case(case_id, session, admin_override=session.get("role") == "admin")
    if not case: raise HTTPException(status_code=404, detail="Case not found")
    return case


@app.patch("/api/cases/{case_id}")
def officer_case_update_api(case_id: int, req: CaseOfficerUpdateRequest, authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization); require_role(session, "officer", "admin")
    try: return officer_update_case(case_id, session, new_status=req.status, remarks=req.remarks)
    except PermissionError as exc: raise HTTPException(status_code=403, detail=str(exc))
    except LookupError as exc: raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc: raise HTTPException(status_code=409, detail=str(exc))


@app.get("/api/admin/overview")
def admin_overview_api(authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization); require_role(session, "admin")
    return admin_overview()


@app.get("/api/admin/analytics")
def admin_analytics_api(days: int = 30, authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization); require_role(session, "admin")
    return admin_analytics(days)


@app.get("/api/admin/officers")
def admin_officers_api(authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization); require_role(session, "admin")
    return list_officers()


@app.get("/api/admin/officers/{actor_id}")
def admin_officer_detail_api(actor_id: str, authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization); require_role(session, "admin")
    detail = officer_detail(actor_id)
    if not detail: raise HTTPException(status_code=404, detail="Officer not found")
    return detail


@app.get("/api/admin/inspections")
def admin_inspections_api(limit: int = 200, authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization); require_role(session, "admin")
    return list_all_inspections(limit=limit)


@app.get("/api/admin/complaints")
def admin_cases_api(limit: int = 200, status: Optional[str] = None, officer: Optional[str] = None, violation_type: Optional[str] = None, priority: Optional[str] = None, product: Optional[str] = None, manufacturer: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization); require_role(session, "admin")
    return list_cases(session, limit=limit, status=status, officer=officer, violation_type=violation_type, priority=priority, product=product, manufacturer=manufacturer, date_from=date_from, date_to=date_to)


@app.get("/api/admin/complaints/{case_id}")
def admin_case_detail_api(case_id: int, authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization); require_role(session, "admin")
    case = get_case(case_id, session, admin_override=True)
    if not case: raise HTTPException(status_code=404, detail="Case not found")
    return case


@app.patch("/api/admin/complaints/{case_id}")
def admin_case_update_api(case_id: int, req: CaseUpdateRequest, authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization); require_role(session, "admin")
    try: return update_case(case_id, session, new_status=req.status, admin_notes=req.admin_notes, priority=req.priority)
    except LookupError as exc: raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc: raise HTTPException(status_code=409, detail=str(exc))


@app.get("/api/admin/audit")
def admin_audit_api(limit: int = 200, authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization); require_role(session, "admin")
    return list_audit(limit)


@app.get("/api/admin/system-health")
def admin_system_health_api(authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization); require_role(session, "admin")
    h = health()
    # health() intentionally exposes no secret values or environment variables.
    return {**h, "database": "ok", "api": "ok"}


@app.post("/api/catalog/confirm")
def confirm_catalog(req: CatalogConfirmRequest, authorization: Optional[str] = Header(default=None)):
    session = require_session(authorization)
    provenance = "staff_confirmed" if session.get("role") in {"officer", "admin"} else "user_confirmed"
    cid = register_or_update_product(
        req.product_name, session_id=session["sid"], company=req.company, quantity=req.quantity,
        fssai=req.fssai, mrp=req.mrp, provenance=provenance,
    )
    return {"ok": True, "catalog_id": cid, "provenance": provenance}


@app.get("/api/catalog")
def catalog(limit: int = 50, authorization: Optional[str] = Header(default=None)):
    require_session(authorization)
    return get_catalog(limit)


@app.get("/admin")
def admin_page():
    return FileResponse(BASE_DIR / "admin.html")


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html")


# Static service worker / manifest / JS and any future assets.
app.mount("/", StaticFiles(directory=str(BASE_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
