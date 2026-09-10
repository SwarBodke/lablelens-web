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
from compliance_engine import (  # noqa: E402
    ComplianceEngine,
    HybridOCR,
    expiry_is_past,
    extract_quantity,
    validate_unit_sale_price,
)
from ruleset import required_font_height_mm  # noqa: E402
from main import app  # noqa: E402


COMPLETE = """Generic Name: Liquid Detergent
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
    q = extract_quantity("Net Qty: 500 gms")
    assert_eq(q["unit"], "g", "legacy unit should normalise")
    assert_eq(q["unit_violation"], "gms", "legacy unit must remain flagged")
    assert expiry_is_past({"month": 9, "year": 2026}, date(2026, 9, 10)) is False
    assert expiry_is_past({"month": 8, "year": 2026}, date(2026, 9, 10)) is True
    assert_eq(required_font_height_mm(50, moulded=True), 2.0, "Table-I A<=50 moulded height")

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
    result = e.analyze_text(COMPLETE, ctx)
    assert_eq(result["compliance_status"], "compliant", "complete calibrated package")
    assert all(v["status"] == "compliant" for v in result["checks"].values())

    missing_care = COMPLETE.split("Consumer Care:", 1)[0]
    result = e.analyze_text(missing_care, ctx)
    assert_eq(result["checks"]["consumer_care"]["status"], "non_compliant", "missing care with complete evidence")
    assert_eq(result["compliance_status"], "non_compliant", "missing care must affect overall result")

    partial = e.analyze_text(missing_care, {**ctx, "evidence_scope": "partial"})
    assert_eq(partial["checks"]["consumer_care"]["status"], "needs_review", "partial photo missing care")
    assert_eq(partial["compliance_status"], "needs_review", "partial evidence must stay conservative")

    ecommerce = e.analyze_text(LISTING, {"source_type": "ecommerce", "evidence_scope": "complete_listing", "category": "general"})
    assert_eq(ecommerce["compliance_status"], "compliant", "complete listing")
    assert ecommerce["ocr_confidence"] is None
    assert_eq(ecommerce["checks"]["date"]["status"], "not_applicable", "ecommerce mfg month/year display")
    assert_eq(ecommerce["checks"]["font_size"]["status"], "not_applicable", "physical font check on ecommerce")

    proviso = validate_unit_sale_price({"value": 1.0, "unit": "L"}, 100.0, None, "complete_package")
    assert_eq(proviso["status"], "not_applicable", "unit sale price proviso")
    mismatch = validate_unit_sale_price({"value": 500.0, "unit": "ml"}, 149.0, {"value": 0.5, "unit": "ml"}, "complete_package")
    assert_eq(mismatch["status"], "non_compliant", "wrong unit sale price")

    # Adaptive OCR routing: strong Tesseract evidence must skip RapidOCR.
    # Weak/incomplete Tesseract evidence must escalate and merge RapidOCR text.
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
    assert_eq(routed["engines_used"]["route"], "tesseract_fast", "fast route label")

    partial_tess = "Generic Name: Liquid Detergent\nManufactured by: Demo Consumer Products Pvt Ltd\nNet Qty: 500 ml"
    rapid_additions = """Plot 21 Industrial Estate Pune Maharashtra 411001
MRP Rs 149 Inclusive of all taxes
Mfg Date: 08/2026
Consumer Care: Helpdesk
Phone: +91 9876543210
Email: care@example.com"""
    o = HybridOCR(); o.mode = "adaptive"
    o._decode_codes = lambda source: []
    calls = []
    additions = {
        "full": partial_tess,
        "bottom": "MRP Rs 149 Inclusive of all taxes\nMfg Date: 08/2026",
        "top": "CLEANWAVE",
        "raw": "",
    }
    def weak_tess(source, name):
        calls.append(name)
        return additions[name], 90.0
    rapid_calls = []
    def fake_rapid(source):
        rapid_calls.append("rapid")
        return rapid_additions, 94.0
    o._run_named_tesseract = weak_tess
    o._rapid_pass = fake_rapid
    routed = o.read(b"x", {"source_type": "physical", "evidence_scope": "complete_package"})
    assert_eq(calls[:2], ["full", "bottom"], "weak label should use targeted Tesseract recovery")
    assert_eq(rapid_calls, ["rapid"], "incomplete Tesseract must escalate to RapidOCR")
    assert_eq(routed["engines_used"]["route"], "tesseract_then_rapid", "escalation route label")
    assert "care@example.com" in routed["text"], "RapidOCR evidence should be merged additively"


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
            json={"text": LISTING, "product_name": "Demo Liquid Detergent", "evidence_scope": "complete_listing", "category": "general"},
        )
        assert_eq(one.status_code, 200, "first listing analysis")
        result = one.json()
        assert_eq(result["compliance_status"], "compliant", "API complete listing status")
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
        assert rows[0].get("record_hash") and rows[1].get("record_hash")

        for fmt, magic in [("pdf", b"%PDF"), ("docx", b"PK")]:
            r = client.get(f"/api/inspections/{first_id}/report.{fmt}", headers=headers)
            assert_eq(r.status_code, 200, f"{fmt} report")
            assert r.content.startswith(magic), f"{fmt} report signature"
        jr = client.get(f"/api/inspections/{first_id}/report.json", headers=headers)
        assert_eq(jr.status_code, 200, "JSON report")
        assert jr.json()["inspection"]["record_hash"] == first_hash

        # New signed workspace cannot read the first workspace's history.
        s2 = client.post("/api/session/guest").json()
        h2 = {"Authorization": f"Bearer {s2['token']}"}
        isolated = client.get("/api/inspections", headers=h2)
        assert_eq(isolated.status_code, 200, "isolated history request")
        assert_eq(isolated.json(), [], "workspace isolation")

        # Exercise the physical image route with one clear synthetic panel. Exact OCR
        # text is intentionally not asserted because engine availability varies by OS.
        import cv2
        import numpy as np
        img = np.full((700, 1200, 3), 255, dtype=np.uint8)
        cv2.putText(img, "NET QTY 500 ml", (50, 180), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(img, "MRP Rs 149", (50, 310), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 0), 4, cv2.LINE_AA)
        ok, encoded = cv2.imencode(".png", img)
        assert ok
        physical = client.post(
            "/api/analyze/multi",
            headers=h2,
            files=[("images", ("panel.png", encoded.tobytes(), "image/png"))],
            data={"evidence_scope": "partial", "category": "general"},
        )
        assert_eq(physical.status_code, 200, "physical OCR route")
        pdata = physical.json()
        assert pdata.get("inspection_id")
        assert len(pdata.get("evidence") or []) == 1
        assert pdata["evidence"][0].get("sha256")
        assert pdata.get("compliance_status") in {"compliant", "non_compliant", "needs_review"}


if __name__ == "__main__":
    logic_tests()
    api_tests()
    print("PASS: LableLens smoke/regression tests")
