"""Generate PDF/DOCX/JSON screening reports without exposing raw OCR transcript.

Raw OCR text remains stored internally for audit/debugging, but report exports
contain structured evidence and rule results only.
"""
from __future__ import annotations

import io
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict

from ruleset import metadata as ruleset_metadata


def _safe(v: Any) -> str:
    if v is None:
        return "Not detected"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, indent=2)
    return str(v)


def _ocr_label(record: Dict[str, Any]) -> str:
    if record.get("source_type") == "ecommerce" and record.get("ocr_confidence") is None:
        return "N/A - direct listing text"
    value = record.get("ocr_confidence")
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.1f}%"
    except Exception:
        return str(value)


def _report_safe_inspection(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy suitable for export, excluding raw transcription fields."""
    safe = deepcopy(record)
    for key in (
        "raw_text",
        "lines",
        "ocr_transcript",
        "transcript",
        "unit_sale_price",
        "unit_sale_price_screen",
    ):
        safe.pop(key, None)

    # Defensive removal from nested check/evidence structures in case an older
    # stored inspection is exported after the ruleset migration.
    checks = safe.get("checks")
    if isinstance(checks, dict):
        for check in checks.values():
            if not isinstance(check, dict):
                continue
            evidence = check.get("evidence")
            if isinstance(evidence, dict):
                evidence.pop("unit_sale_price", None)
                evidence.pop("unit_sale_price_screen", None)
            subs = check.get("subchecks")
            if isinstance(subs, list):
                check["subchecks"] = [
                    sub for sub in subs
                    if not (isinstance(sub, dict) and str(sub.get("name") or "").strip().lower() == "unit sale price")
                ]
    return safe


def report_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "report_generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "report_type": "Legal Metrology screening aid - not an enforcement order",
        "ruleset": ruleset_metadata(),
        "inspection": _report_safe_inspection(record),
    }


def _audit_rows(record: Dict[str, Any]):
    record_hash = record.get("record_hash") or (record.get("audit") or {}).get("record_hash")
    previous_hash = record.get("previous_hash") or (record.get("audit") or {}).get("previous_hash")
    if not record_hash:
        return []
    return [
        ["Audit algorithm", "SHA-256 per-session record chain"],
        ["Record hash", record_hash],
        ["Previous record hash", previous_hash or "GENESIS"],
    ]


def _checks(record: Dict[str, Any]) -> Dict[str, Any]:
    checks = record.get("checks") or {}
    if isinstance(checks, str):
        try:
            checks = json.loads(checks)
        except Exception:
            checks = {}
    return checks if isinstance(checks, dict) else {}


def _evidence(record: Dict[str, Any]):
    evidence = record.get("evidence") or []
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except Exception:
            evidence = []
    return evidence if isinstance(evidence, list) else []


def build_pdf(record: Dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, rightMargin=34, leftMargin=34, topMargin=32, bottomMargin=32)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Small", parent=styles["BodyText"], fontSize=8.5, leading=11))
    rules = ruleset_metadata()
    story = [
        Paragraph("LableLens - Legal Metrology Screening Report", styles["Title"]),
        Paragraph(f"Inspection INS-{record.get('id', '-')}", styles["Heading2"]),
        Paragraph(
            "Screening against the original Legal Metrology (Packaged Commodities) Rules, 2011 baseline notified by G.S.R. 202(E). "
            "This historical baseline intentionally excludes later amendments and is not an enforcement order or a statement of the fully amended current law.",
            styles["Small"],
        ),
        Spacer(1, 10),
    ]
    status = record.get("compliance_status") or record.get("status") or "needs_review"
    summary_rows = [
        ["Product", _safe(record.get("product_name"))],
        ["Status", str(status).replace("_", " ").title()],
        ["Source", str(record.get("source_type") or "physical").replace("_", " ").title()],
        ["Evidence scope", str(record.get("evidence_scope") or "partial").replace("_", " ").title()],
        ["Created", _safe(record.get("created_at"))],
        ["OCR confidence", _ocr_label(record)],
        ["Extraction completeness", f"{record.get('extraction_completeness', 0)}%"],
        ["Ruleset", record.get("ruleset_id") or rules["id"]],
    ] + _audit_rows(record)
    t = Table(summary_rows, colWidths=[130, 380])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#d8dee8")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f4f6f8")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("LEADING", (0, 0), (-1, -1), 10.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.extend([t, Spacer(1, 14), Paragraph("Six rule-engine groups", styles["Heading2"])])

    for key, item in _checks(record).items():
        if not isinstance(item, dict):
            continue
        title = item.get("title") or key.replace("_", " ").title()
        st = item.get("status", "needs_review").replace("_", " ").title()
        story.append(Paragraph(f"{title} - {st}", styles["Heading3"]))
        story.append(Paragraph(_safe(item.get("summary") or item.get("evidence") or ""), styles["Small"]))
        if item.get("rule"):
            story.append(Paragraph(f"Reference: {_safe(item.get('rule'))}", styles["Small"]))
        for issue in item.get("issues") or []:
            escaped = str(issue).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            story.append(Paragraph(f"- {escaped}", styles["Small"]))
        story.append(Spacer(1, 5))

    evidence = _evidence(record)
    if evidence:
        story.extend([Spacer(1, 10), Paragraph("Image evidence hashes", styles["Heading2"])])
        for ev in evidence:
            if isinstance(ev, dict):
                story.append(Paragraph(f"SHA-256: {_safe(ev.get('sha256'))} ({ev.get('bytes', 0)} bytes)", styles["Small"]))

    # Intentionally no OCR transcription/raw-text section in exported reports.
    doc.build(story)
    return out.getvalue()


def build_docx(record: Dict[str, Any]) -> bytes:
    from docx import Document

    doc = Document()
    doc.add_heading("LableLens - Legal Metrology Screening Report", level=0)
    doc.add_paragraph(f"Inspection INS-{record.get('id', '-')}")
    doc.add_paragraph(
        "Screening against the original Legal Metrology (Packaged Commodities) Rules, 2011 baseline notified by G.S.R. 202(E). "
        "This historical baseline intentionally excludes later amendments and is not an enforcement order or a statement of the fully amended current law."
    )
    table = doc.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    status = record.get("compliance_status") or record.get("status") or "needs_review"
    summary = [
        ("Product", record.get("product_name")),
        ("Status", status.replace("_", " ").title()),
        ("Source", (record.get("source_type") or "physical").replace("_", " ").title()),
        ("Evidence scope", (record.get("evidence_scope") or "partial").replace("_", " ").title()),
        ("Created", record.get("created_at")),
        ("OCR confidence", _ocr_label(record)),
        ("Extraction completeness", f"{record.get('extraction_completeness', 0)}%"),
        ("Ruleset", record.get("ruleset_id") or ruleset_metadata()["id"]),
    ]
    for k, v in summary:
        cells = table.add_row().cells
        cells[0].text = str(k)
        cells[1].text = _safe(v)

    audit = _audit_rows(record)
    if audit:
        doc.add_heading("Audit chain", level=1)
        for key, value in audit:
            doc.add_paragraph(f"{key}: {value}")

    doc.add_heading("Six rule-engine groups", level=1)
    for key, item in _checks(record).items():
        if not isinstance(item, dict):
            continue
        doc.add_heading(
            f"{item.get('title') or key} - {item.get('status', 'needs_review').replace('_', ' ').title()}",
            level=2,
        )
        doc.add_paragraph(_safe(item.get("summary") or item.get("evidence") or ""))
        if item.get("rule"):
            doc.add_paragraph(f"Reference: {item.get('rule')}")
        for issue in item.get("issues") or []:
            doc.add_paragraph(str(issue), style="List Bullet")

    evidence = _evidence(record)
    if evidence:
        doc.add_heading("Image evidence hashes", level=1)
        for ev in evidence:
            if isinstance(ev, dict):
                doc.add_paragraph(f"SHA-256: {ev.get('sha256')} ({ev.get('bytes', 0)} bytes)", style="List Bullet")

    # Intentionally no OCR transcription/raw-text section in exported reports.
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()
