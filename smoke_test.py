"""Self-contained smoke/regression tests for the LableLens build.

Run: python smoke_test.py
No pytest dependency is required.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path

_tmp = tempfile.TemporaryDirectory(prefix="lablelens-test-")
os.environ["SQLITE_PATH"] = str(Path(_tmp.name) / "test.db")
os.environ["EVIDENCE_DIR"] = str(Path(_tmp.name) / "evidence")
os.environ["LABLELENS_SECRET"] = "test-only-secret-do-not-use-in-production"
os.environ["GUEST_SCAN_LIMIT"] = "2"
os.environ["PRESERVE_EVIDENCE"] = "true"
os.environ.setdefault("LABLELENS_OCR_LANGS", "eng")

from fastapi.testclient import TestClient  # noqa: E402
from compliance_engine import ComplianceEngine, HybridOCR, expiry_is_past, extract_quantity  # noqa: E402
from inspection_rule_contract import finalize_six_group_result  # noqa: E402
from reports import report_payload  # noqa: E402
from ruleset import (  # noqa: E402
    RULESET_ID,
    metadata as ruleset_metadata,
    required_original_letter_height_mm,
    required_original_numeral_height_mm,
)
from main import app  # noqa: E402


# The input deliberately contains a Unit Sale Price line.  The selected original
# 2011 ruleset must ignore it: it must not become a check, score or report field.
COMPLETE = """Liquid Detergent
Manufactured by: Demo Consumer Products Pvt. Ltd.
Plot 21, Industrial Estate, Pune, Maharashtra - 411001
Net Qty: 500 ml
MRP: Rs. 149.00 Inclusive of all taxes
Unit Sale Price: Rs. 0.30 per ml
Mfg Date: 08/2026
Consumer Care: Demo Consumer Products Pvt. Ltd.
Plot 21, Industrial Estate, Pune, Maharashtra - 411001
Phone: +91 9876543210 Email: care@example.com"""

LISTING = COMPLETE.replace("\nMfg Date: 08/2026", "")


def assert_eq(actual, expected, message):
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def logic_tests():
    meta = ruleset_metadata()
    assert_eq(meta["id"], RULESET_ID, "ruleset metadata id")
    assert meta.get("historical_baseline") is True
    assert meta.get("excluded_later_amendments"), "later-amendment exclusions should be explicit"
    assert "unit sale price" not in meta["checks"]["mrp"]["title"].lower()
    assert "Rule 6(11)" not in meta["checks"]["mrp"]["rules"]

    q = extract_quantity("Net Qty: 500 gms")
    assert_eq(q["unit"], "g", "legacy unit should normalise")
    assert_eq(q["unit_violation"], "gms", "legacy unit must remain flagged")
    assert expiry_is_past({"month": 9, "year": 2026}, date(2026, 9, 10)) is False
    assert expiry_is_past({"month": 8, "year": 2026}, date(2026, 9, 10)) is True

    # Original Rule 7 Table-I weight/volume thresholds.
    assert_eq(required_original_numeral_height_mm(200, "g", None, False), 1.0, "Table-I <=200 g")
    assert_eq(required_original_numeral_height_mm(500, "ml", None, False), 2.0, "Table-I <=500 ml")
    assert_eq(required_original_numeral_height_mm(1, "kg", None, False), 4.0, "Table-I >500 g")
    assert_eq(required_original_letter_height_mm(False), 1.0, "original letter height")
    assert_eq(required_original_letter_height_mm(True), 2.0, "original formed-letter height")

    e = ComplianceEngine()
    ctx = {
        "source_type": "physical",
        "evidence_scope": "complete_package",
        "category": "general",
        "pdp_area_cm2": 120,
        "font_height_mm": 3.0,
        "font_width_mm": 1.2,
        "moulded_text": False,
    }
    result = finalize_six_group_result(e.analyze_text(COMPLETE, ctx))
    assert_eq(result["ruleset_id"], RULESET_ID, "original 2011 ruleset applied")
    assert_eq(result["compliance_status"], "compliant", "complete calibrated package")
    assert_eq(list(result["checks"]), ["responsible_entity", "net_quantity", "date", "mrp", "consumer_care", "font_size"], "six-group order")
    assert "unit_sale_price" not in result
    assert all("unit sale price" not in str(v).lower() for v in result["checks"].values())

    # Product/commodity identity can satisfy the grouped Rule 6(1)(b) sub-check;
    # the UI does not need a separate literal "Generic Name" field caption.
    identity_subs = result["checks"]["responsible_entity"]["subchecks"]
    identity = [x for x in identity_subs if x["name"].startswith("Commodity identity")][0]
    assert_eq(identity["status"], "compliant", "commodity identity sub-check")

    missing_care = COMPLETE.split("Consumer Care:", 1)[0]
    result = finalize_six_group_result(e.analyze_text(missing_care, ctx))
    assert_eq(result["checks"]["consumer_care"]["status"], "non_compliant", "missing care with complete physical evidence")
    assert_eq(result["compliance_status"], "non_compliant", "missing care affects overall result")

    partial = finalize_six_group_result(e.analyze_text(missing_care, {**ctx, "evidence_scope": "partial"}))
    assert_eq(partial["checks"]["consumer_care"]["status"], "needs_review", "partial photo missing care")
    assert_eq(partial["compliance_status"], "needs_review", "partial evidence stays conservative")

    # The original 2011 Rules did not contain the later e-commerce display
    # provision, so missing package evidence in listing text stays REVIEW rather
    # than being treated as an automatic package-law FAIL or invented N/A.
    ecommerce = finalize_six_group_result(e.analyze_text(LISTING, {"source_type": "ecommerce", "evidence_scope": "complete_listing", "category": "general"}))
    assert_eq(ecommerce["compliance_status"], "needs_review", "historical baseline listing conservatism")
    assert ecommerce["ocr_confidence"] is None
    assert_eq(ecommerce["checks"]["date"]["status"], "needs_review", "listing absence is not a 2011 package FAIL")
    assert_eq(ecommerce["checks"]["font_size"]["status"], "not_applicable", "physical font measurement from listing text")

    # E-mail was qualified by "if available" in original Rule 6(2), so it does
    # not independently fail the consumer-care group.
    no_email = COMPLETE.replace(" Email: care@example.com", "")
    no_email_result = finalize_six_group_result(e.analyze_text(no_email, ctx))
    email_sub = [x for x in no_email_result["checks"]["consumer_care"]["subchecks"] if x["name"].startswith("E-mail")][0]
    assert_eq(email_sub["status"], "not_applicable", "optional e-mail absence")

    # Report JSON must not expose the raw OCR/listing transcript.
    export = report_payload(result)
    assert "raw_text" not in export["inspection"]
    assert "lines" not in export["inspection"]
    assert "unit_sale_price" not in export["inspection"]

    # Adaptive OCR routing: strong Tesseract evidence must skip RapidOCR.
    o = HybridOCR(); o.mode = "adaptive"
    o._decode_codes = lambda source: []
    calls = []
    def strong_tess(source, name):
        calls.append(name)
        if name != "full":
            raise AssertionError(f"unexpected Tesseract recovery pass: {name}")
        return COMPLETE, 95.0
    o._run_named_tesseract = strong_tess
    o._rapid_pass = lambda source: (_ for _ in ()).throw(AssertionError("unexpected RapidOCR escalation"))
    routed = o.read(b"x", {"source_type": "physical", "evidence_scope": "complete_package"})
    assert_eq(calls, ["full"], "strong flat label should use one Tesseract pass")
    assert_eq(routed["engines_used"]["fast_path_accepted"], True, "strong Tesseract should pass strict gate")
    assert_eq(routed["engines_used"]["rapidocr"], False, "strong Tesseract should skip RapidOCR")


def api_tests():
    with TestClient(app) as client:
        s = client.post("/api/session/guest")
        assert_eq(s.status_code, 200, "guest session")
        session = s.json()
        assert_eq(session["scan_limit"], 2, "guest scan limit")
        headers = {"Authorization": f"Bearer {session['token']}"}

        one = client.post(
            "/api/analyze/text",
            headers=headers,
            json={"text": COMPLETE, "product_name": "Demo Liquid Detergent", "evidence_scope": "complete_listing", "category": "general"},
        )
        assert_eq(one.status_code, 200, "listing analysis")
        result = one.json()
        assert_eq(result["ruleset_id"], RULESET_ID, "API ruleset id")
        assert "unit_sale_price" not in result
        assert result.get("audit", {}).get("record_hash"), "record hash should be returned immediately"
        first_hash = result["audit"]["record_hash"]
        first_id = result["inspection_id"]

        two = client.post(
            "/api/analyze/text",
            headers=headers,
            json={"text": LISTING.replace("Consumer Care:", "Care Desk:"), "product_name": "Demo Product 2", "evidence_scope": "partial", "category": "general"},
        )
        assert_eq(two.status_code, 200, "second listing analysis")
        second = two.json()
        assert_eq(second.get("audit", {}).get("previous_hash"), first_hash, "record chain linkage")

        blocked = client.post(
            "/api/analyze/text",
            headers=headers,
            json={"text": LISTING, "evidence_scope": "complete_listing", "category": "general"},
        )
        assert_eq(blocked.status_code, 429, "guest scan cap")

        hist = client.get("/api/inspections", headers=headers)
        assert_eq(hist.status_code, 200, "history")
        rows = hist.json()
        assert_eq(len(rows), 2, "session history count")

        for fmt, magic in [("pdf", b"%PDF"), ("docx", b"PK")]:
            r = client.get(f"/api/inspections/{first_id}/report.{fmt}", headers=headers)
            assert_eq(r.status_code, 200, f"{fmt} report")
            assert r.content.startswith(magic), f"{fmt} report signature"
            # Raw transcript must not be embedded in human-readable reports.
            assert b"Transcription / listing evidence" not in r.content

        jr = client.get(f"/api/inspections/{first_id}/report.json", headers=headers)
        assert_eq(jr.status_code, 200, "JSON report")
        payload = jr.json()
        assert payload["inspection"]["record_hash"] == first_hash
        assert "raw_text" not in payload["inspection"]
        assert "lines" not in payload["inspection"]
        assert "unit_sale_price" not in payload["inspection"]


if __name__ == "__main__":
    logic_tests()
    api_tests()
    print("PASS: LableLens original-2011 ruleset smoke/regression tests")
