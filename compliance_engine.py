"""LableLens OCR + six-core Legal Metrology screening engine.

Design goals:
- preserve original OCR evidence; never silently 'correct' a legal declaration
- report actual OCR confidence separately from extraction completeness
- use tri-state outcomes: compliant / non_compliant / needs_review
- distinguish partial evidence from a user-confirmed complete package/listing
- keep physical font-size checks calibrated rather than inferred from pixels alone
"""
from __future__ import annotations

import calendar
import json
import math
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import pytesseract

from label_ocr import Extractor, Patterns, Preprocessor, Config as LegacyConfig
from ruleset import CHECKS, RULESET_ID, CATEGORY_PROFILES, required_font_height_mm


STANDARD_UNIT_MAP = {
    "mg": "mg", "g": "g", "gm": "g", "kg": "kg",
    "ml": "ml", "l": "L", "litre": "L", "liter": "L",
    "cm": "cm", "m": "m", "metre": "m", "meter": "m",
    "pc": "pcs", "pcs": "pcs", "piece": "pcs", "pieces": "pcs",
    "no": "pcs", "nos": "pcs", "number": "pcs",
    "unit": "pcs", "units": "pcs", "n": "pcs",
}
NON_STANDARD_UNIT_MAP = {
    "gms": "g", "grm": "g", "grms": "g", "kgs": "kg",
    "ltr": "L", "ltrs": "L", "lts": "L", "mls": "ml",
    "cms": "cm", "mtrs": "m", "mts": "m",
}
ALL_UNIT_TOKENS = sorted(set(STANDARD_UNIT_MAP) | set(NON_STANDARD_UNIT_MAP), key=len, reverse=True)
UNIT_ALT = "|".join(re.escape(x) for x in ALL_UNIT_TOKENS)

DECLARATION_STOP = re.compile(
    r"\b(?:MRP|M\.R\.P|Net\s*(?:Qty|Quantity|Wt|Weight)|Mfg|Mfd|Packed|PKD|Expiry|Exp\.?|Best\s*Before|Use\s*Before|"
    r"FSSAI|Batch|Lot|Ingredients?|Customer\s*Care|Consumer\s*Care|Helpline|Email|E-mail|Phone|Tel(?:ephone)?)\b",
    re.I,
)
ADDRESS_HINTS = re.compile(
    r"\b(?:plot|house|h\.?no|flat|floor|building|bldg|road|rd\.?|street|st\.?|lane|sector|phase|industrial|estate|"
    r"village|post|po\b|district|dist\.?|taluk|tehsil|nagar|colony|near|opp(?:osite)?|state|india|pin\s*code|pincode)\b",
    re.I,
)
PIN_RE = re.compile(r"\b[1-9]\d{5}\b")
PHONE_RE = re.compile(
    r"(?:\+?91[\s\-]?)?(?:[6-9]\d{4}[\s\-]?\d{5}|0?\d{2,4}[\s\-]?\d{6,8})\b"
    r"|\b1[\s\-]?800[\s\-]?\d{2,4}[\s\-]?\d{3,4}\b"
)
EMAIL_RE = re.compile(r"[\w.\-+]+@[\w.\-]+\.\w{2,}", re.I)
CARE_MARKER = re.compile(r"\b(?:consumer\s*care|customer\s*care|consumer\s*complaints?|customer\s*support|helpline|grievance)\b", re.I)
ROLE_MARKER = re.compile(r"\b(?:manufactured|manufacturer|mfg|packed|packer|marketed|distributed|imported|importer|made)\s*(?:by|at|for)?\b", re.I)
COO_RE = re.compile(r"(?:country\s*of\s*(?:origin|manufacture|assembly)|made\s+in)\s*[:\-]?\s*([A-Za-z][A-Za-z .&\-]{2,40})", re.I)
GENERIC_RE = re.compile(r"(?:generic\s*name(?:\s*of\s*(?:the\s*)?commodity)?|common\s*name|name\s*of\s*(?:the\s*)?commodity|commodity)\s*[:\-]?\s*([^\n;]{2,80})", re.I)
UNIT_PRICE_PATTERNS = [
    re.compile(r"(?:unit\s*sale\s*price|unit\s*price)\s*[:\-]?\s*(?:₹|Rs\.?\s*)?(\d+(?:[.,]\d{1,2})?)\s*(?:/|per)\s*([A-Za-z]+)", re.I),
    re.compile(r"(?:₹|Rs\.?\s*)(\d+(?:[.,]\d{1,2})?)\s*(?:/|per)\s*(g|kg|ml|l|litre|liter|cm|m|metre|meter|number|unit|piece|pc|pcs)\b", re.I),
]


def _clean_line(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip(" \t,;:-")


def _dedupe_lines(*texts: str) -> str:
    seen, out = set(), []
    for text in texts:
        for raw in (text or "").splitlines():
            line = _clean_line(raw)
            key = line.casefold()
            if len(line) > 1 and key not in seen:
                seen.add(key)
                out.append(line)
    return "\n".join(out)


def _status_for_missing(evidence_scope: str) -> str:
    return "non_compliant" if evidence_scope in {"complete_package", "complete_listing"} else "needs_review"


def _issue_check(key: str, status: str, summary: str, evidence: Any = None, issues: Optional[List[str]] = None, subchecks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    meta = CHECKS[key]
    return {
        "id": key,
        "number": meta["number"],
        "title": meta["title"],
        "rule": meta["rules"],
        "status": status,
        "summary": summary,
        "evidence": evidence,
        "issues": issues or [],
        "subchecks": subchecks or [],
    }


class HybridOCR:
    def __init__(self):
        self.pre = Preprocessor(LegacyConfig())
        self.legacy_extractor = Extractor()
        self.tesseract_cmd = self._configure_tesseract()
        self.languages = self._resolve_languages()
        self._rapid = None
        self._rapid_checked = False
        # adaptive (default): a fast, single-pass Tesseract read runs first.
        # Strict confidence + evidence gates allow easy/flat labels to finish
        # immediately. Missing or uncertain evidence escalates to targeted
        # Tesseract zones and then RapidOCR/ONNX. The merge is additive: a
        # recovery engine never replaces text already recovered by Tesseract.
        # ensemble forces every OCR path for accuracy regression testing.
        self.mode = (os.getenv("LABLELENS_OCR_MODE", "adaptive") or "adaptive").strip().lower()
        if self.mode not in {"adaptive", "ensemble", "rapid_first", "rapid_only", "tesseract_only"}:
            self.mode = "adaptive"
        self.tesseract_fast_confidence_threshold = float(os.getenv("LABLELENS_TESSERACT_FAST_CONFIDENCE_THRESHOLD", "86"))
        self.tesseract_fast_coverage_threshold = float(os.getenv("LABLELENS_TESSERACT_FAST_COVERAGE_THRESHOLD", "0.86"))
        self.tesseract_min_chars = max(20, int(os.getenv("LABLELENS_TESSERACT_MIN_CHARS", "55")))
        self.rapid_confidence_threshold = float(os.getenv("LABLELENS_RAPID_CONFIDENCE_THRESHOLD", "80"))
        self.rapid_coverage_threshold = float(os.getenv("LABLELENS_RAPID_COVERAGE_THRESHOLD", "0.80"))
        self.accuracy_guard = str(os.getenv("LABLELENS_OCR_ACCURACY_GUARD", "true")).strip().lower() not in {"0", "false", "no", "off"}
        self.tesseract_timeout = max(2.0, float(os.getenv("LABLELENS_TESSERACT_TIMEOUT_SEC", "8")))

    @staticmethod
    def _configure_tesseract() -> Optional[str]:
        configured = os.getenv("TESSERACT_CMD", "").strip()
        candidates = [configured] if configured else []
        candidates += [shutil.which("tesseract") or ""]
        if os.name == "nt":
            candidates += [
                r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            ]
        for path in candidates:
            if path and os.path.isfile(path):
                pytesseract.pytesseract.tesseract_cmd = path
                return path
        # Keep executable name for PATH resolution; calls may still fail with a useful error.
        pytesseract.pytesseract.tesseract_cmd = configured or "tesseract"
        return shutil.which("tesseract")

    def _resolve_languages(self) -> str:
        requested = [x for x in os.getenv("LABLELENS_OCR_LANGS", "eng+hin").split("+") if x]
        try:
            available = set(pytesseract.get_languages(config=""))
        except Exception:
            available = {"eng"}
        chosen = [x for x in requested if x in available]
        if not chosen:
            chosen = ["eng"] if "eng" in available else list(available)[:1]
        return "+".join(chosen) if chosen else "eng"

    def _get_rapid(self):
        if self._rapid_checked:
            return self._rapid
        self._rapid_checked = True
        self._rapid_api = None
        errors = []
        try:
            # Current RapidOCR package. The older rapidocr_onnxruntime package is
            # being retired, so new deployments should use rapidocr + onnxruntime.
            from rapidocr import RapidOCR
            self._rapid = RapidOCR()
            self._rapid_api = "modern"
            return self._rapid
        except Exception as exc:
            errors.append(f"rapidocr: {exc}")
        try:
            # Backward-compatible fallback for existing local environments.
            from rapidocr_onnxruntime import RapidOCR
            self._rapid = RapidOCR()
            self._rapid_api = "legacy"
            return self._rapid
        except Exception as exc:
            errors.append(f"rapidocr_onnxruntime: {exc}")
        print("[OCR] RapidOCR unavailable: " + " | ".join(errors))
        self._rapid = None
        return None

    def _tess(self, image: np.ndarray, psm: int) -> Tuple[str, Optional[float]]:
        """One Tesseract invocation per pass, with line reconstruction and timeout.

        The previous implementation called Tesseract twice for every pass
        (image_to_data + image_to_string).  Reconstructing lines from TSV data
        preserves declaration blocks while roughly halving subprocess work.
        """
        config = f"--oem 3 --psm {psm}"
        try:
            data = pytesseract.image_to_data(
                image, lang=self.languages, config=config,
                output_type=pytesseract.Output.DICT, timeout=self.tesseract_timeout,
            )
            confs: List[float] = []
            grouped: Dict[Tuple[int, int, int, int], List[str]] = {}
            n = len(data.get("text", []))
            for i in range(n):
                text = (data.get("text", [""] * n)[i] or "").strip()
                if not text:
                    continue
                try:
                    c = float(data.get("conf", [-1] * n)[i])
                except Exception:
                    c = -1
                if c >= 0:
                    confs.append(c)
                key = (
                    int(data.get("page_num", [1] * n)[i] or 1),
                    int(data.get("block_num", [0] * n)[i] or 0),
                    int(data.get("par_num", [0] * n)[i] or 0),
                    int(data.get("line_num", [0] * n)[i] or 0),
                )
                grouped.setdefault(key, []).append(text)
            lines = [" ".join(grouped[k]).strip() for k in sorted(grouped)]
            text = "\n".join(x for x in lines if x)
            confidence = round(sum(confs) / len(confs), 1) if confs else None
            return text, confidence
        except RuntimeError as exc:
            print(f"[OCR] Tesseract pass psm={psm} timed out/failed: {exc}")
            return "", None
        except Exception as exc:
            print(f"[OCR] Tesseract pass psm={psm} failed: {exc}")
            return "", None

    def _rapid_pass(self, source) -> Tuple[str, Optional[float]]:
        engine = self._get_rapid()
        if engine is None:
            return "", None
        try:
            img = self.pre._load_image(source)
            h, w = img.shape[:2]
            if max(h, w) > 1600:
                scale = 1600.0 / max(h, w)
                img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

            lines, confs = [], []
            if getattr(self, "_rapid_api", None) == "modern":
                result = engine(img)
                for text in (getattr(result, "txts", None) or []):
                    if text:
                        lines.append(str(text).strip())
                for score in (getattr(result, "scores", None) or []):
                    try:
                        c = float(score)
                        if c <= 1.0:
                            c *= 100.0
                        confs.append(c)
                    except Exception:
                        pass
            else:
                result, _ = engine(img)
                for item in result or []:
                    if len(item) >= 2 and item[1]:
                        lines.append(str(item[1]).strip())
                    if len(item) >= 3:
                        try:
                            c = float(item[2])
                            if c <= 1.0:
                                c *= 100.0
                            confs.append(c)
                        except Exception:
                            pass
            return "\n".join(lines), (round(sum(confs) / len(confs), 1) if confs else None)
        except Exception as exc:
            print(f"[OCR] RapidOCR pass failed: {exc}")
            return "", None

    def _decode_codes(self, source) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        try:
            img = self.pre._load_image(source)
        except Exception:
            return out
        # QR works with standard OpenCV.
        try:
            detector = cv2.QRCodeDetector()
            ok, decoded, points, _ = detector.detectAndDecodeMulti(img)
            if ok:
                for value in decoded or []:
                    if value:
                        out.append({"type": "qr", "value": value})
            else:
                value, _, _ = detector.detectAndDecode(img)
                if value:
                    out.append({"type": "qr", "value": value})
        except Exception:
            pass
        # BarcodeDetector is available in opencv-contrib builds.
        try:
            detector = cv2.barcode_BarcodeDetector()
            ok, decoded_info, decoded_type, _ = detector.detectAndDecode(img)
            if ok:
                for value, typ in zip(decoded_info or [], decoded_type or []):
                    if value:
                        out.append({"type": str(typ or "barcode").lower(), "value": value})
        except Exception:
            pass
        # Deduplicate.
        seen, dedup = set(), []
        for item in out:
            key = (item["type"], item["value"])
            if key not in seen:
                seen.add(key); dedup.append(item)
        return dedup

    def _coverage(self, text: str, source_type: str = "physical") -> Dict[str, Any]:
        """Estimate whether OCR-readable declaration evidence is already sufficient.

        This is an OCR-routing score, not a legal-compliance score. It only decides
        whether a slower Tesseract recovery pass is worth running.
        """
        text = text or ""
        ex = self.legacy_extractor
        qty = extract_quantity(text)
        company = ex.extract_company(text)
        address = extract_address(text, company)
        care = extract_consumer_care(text, address.get("address") if address.get("complete") else None)
        generic = extract_generic_name(text) or ex.extract_product_name(text)
        signals = {
            "mrp": ex.extract_mrp(text) is not None,
            "quantity": bool(qty.get("display")),
            "company": bool(company),
            "address": bool(address.get("complete")),
            "consumer_marker": bool(care.get("marker_found")),
            "consumer_contact": bool(care.get("phone") or care.get("email")),
            "inclusive_tax": bool(ex.check_inclusive_tax(text)),
            "generic_name": bool(generic),
            "mfg_date": bool(ex.extract_mfg_date(text)),
        }
        weights = {
            "mrp": 0.14, "quantity": 0.14, "company": 0.14, "address": 0.12,
            "consumer_marker": 0.08, "consumer_contact": 0.10,
            "inclusive_tax": 0.08, "generic_name": 0.08, "mfg_date": 0.12,
        }
        if source_type == "ecommerce":
            # Rule 6(10) does not require manufacture/packing month-year to be
            # displayed on the e-commerce network listing itself.
            weights.pop("mfg_date", None)
        denom = sum(weights.values()) or 1.0
        score = sum(w for k, w in weights.items() if signals.get(k)) / denom
        missing = [k for k in weights if not signals.get(k)]
        return {"score": round(score, 3), "signals": signals, "missing": missing}

    def _run_named_tesseract(self, source, name: str) -> Tuple[str, Optional[float]]:
        if name == "full":
            image, _ = self.pre.preprocess_full(source); return self._tess(image, 11)
        if name == "bottom":
            image, _ = self.pre.preprocess_bottom_zone(source); return self._tess(image, 6)
        if name == "top":
            image, _ = self.pre.preprocess_top_zone(source); return self._tess(image, 7)
        if name == "raw":
            image = self.pre._load_image(source)
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            return self._tess(image, 3)
        raise ValueError(f"Unknown Tesseract pass: {name}")

    def _select_tesseract_recovery_passes(self, coverage: Dict[str, Any]) -> List[str]:
        """Choose cheap Tesseract zone passes after the initial full-label pass."""
        missing = set(coverage.get("missing") or [])
        selected: List[str] = []
        # Bottom-zone enhancement targets common MRP/date/quantity print zones.
        if missing & {"mrp", "quantity", "inclusive_tax", "mfg_date"}:
            selected.append("bottom")
        # Top zone is cheap and helps product/generic-name recovery.
        if "generic_name" in missing:
            selected.append("top")
        return selected[:2]

    def _safe_to_skip_rapid(
        self,
        text: str,
        confidence: Optional[float],
        coverage: Dict[str, Any],
        context: Optional[Dict[str, Any]],
    ) -> Tuple[bool, List[str]]:
        """Conservative fast-path gate.

        This gate only decides whether RapidOCR can be skipped. It never turns
        missing legal declarations into a compliant verdict. To protect accuracy,
        the fast path requires strong Tesseract confidence, enough readable text,
        high declaration coverage, and core evidence anchors.
        """
        reasons: List[str] = []
        conf = float(confidence) if confidence is not None else 0.0
        if conf < self.tesseract_fast_confidence_threshold:
            reasons.append("tesseract_confidence_below_threshold")
        alnum_chars = sum(ch.isalnum() for ch in (text or ""))
        if alnum_chars < self.tesseract_min_chars:
            reasons.append("ocr_text_too_sparse")
        if float(coverage.get("score") or 0.0) < self.tesseract_fast_coverage_threshold:
            reasons.append("declaration_coverage_below_threshold")

        signals = coverage.get("signals") or {}
        # These anchors are widely useful across packaged-commodity panels and are
        # deliberately stricter than the legal verdict itself. If any is not read,
        # RapidOCR gets a chance to recover it rather than accepting a fast miss.
        source_type = (context or {}).get("source_type", "physical")
        evidence_scope = (context or {}).get("evidence_scope", "partial")
        if source_type == "physical":
            anchor_names = ["mrp", "quantity", "company"]
            if evidence_scope in {"complete", "complete_package", "all_panels", "complete_evidence"}:
                anchor_names += ["address", "consumer_marker", "consumer_contact", "mfg_date"]
            missing_anchors = [name for name in anchor_names if not signals.get(name)]
            if missing_anchors:
                reasons.append("missing_core_anchors:" + ",".join(missing_anchors))

        return (not reasons), reasons

    def _average_confidence(self, items: Sequence[Optional[float]]) -> Optional[float]:
        vals = [float(x) for x in items if x is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    def read(self, source, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Accuracy-preserving adaptive hybrid OCR.

        Modes (LABLELENS_OCR_MODE):
          adaptive       Tesseract fast path -> targeted recovery -> RapidOCR (default)
          ensemble       RapidOCR + all four Tesseract passes (accuracy regression)
          rapid_first    legacy v3 RapidOCR-first adaptive path for comparison
          rapid_only     RapidOCR only
          tesseract_only all four Tesseract passes
        """
        from concurrent.futures import ThreadPoolExecutor

        started = time.perf_counter()
        context = dict(context or {})
        source_type = context.get("source_type", "physical")
        tess_results: Dict[str, Tuple[str, Optional[float]]] = {}
        rapid_text, rapid_conf = ("", None)
        rapid_elapsed = 0.0
        tess_elapsed = 0.0
        rapid_attempted = False
        escalation_reason: Optional[str] = None
        fast_path_accepted = False
        gate_reasons: List[str] = []

        # Diagnostic / regression modes first.
        if self.mode == "rapid_only":
            t0 = time.perf_counter()
            rapid_attempted = True
            rapid_text, rapid_conf = self._rapid_pass(source)
            rapid_elapsed = time.perf_counter() - t0

        elif self.mode == "tesseract_only":
            t0 = time.perf_counter()
            names = ["full", "bottom", "top", "raw"]
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {name: pool.submit(self._run_named_tesseract, source, name) for name in names}
                for name in names:
                    tess_results[name] = futures[name].result()
            tess_elapsed = time.perf_counter() - t0
            escalation_reason = "tesseract_only"

        elif self.mode == "ensemble":
            t0 = time.perf_counter()
            rapid_attempted = True
            names = ["full", "bottom", "top", "raw"]
            with ThreadPoolExecutor(max_workers=3) as pool:
                rapid_future = pool.submit(self._rapid_pass, source)
                futures = {name: pool.submit(self._run_named_tesseract, source, name) for name in names}
                rapid_text, rapid_conf = rapid_future.result()
                for name in names:
                    tess_results[name] = futures[name].result()
            elapsed = time.perf_counter() - t0
            # Per-engine time overlaps in ensemble mode; total is still exact.
            rapid_elapsed = elapsed
            tess_elapsed = elapsed
            escalation_reason = "forced_ensemble"

        elif self.mode == "rapid_first":
            # Retain the previous v3 strategy for diagnostics and A/B comparison.
            t0 = time.perf_counter()
            rapid_attempted = True
            rapid_text, rapid_conf = self._rapid_pass(source)
            rapid_elapsed = time.perf_counter() - t0
            rapid_cov = self._coverage(rapid_text, source_type=source_type) if rapid_text else {"score": 0.0, "signals": {}, "missing": []}
            if not rapid_text:
                selected = ["full", "bottom", "top", "raw"]
                escalation_reason = "rapidocr_unavailable_or_empty"
            elif rapid_conf is not None and rapid_conf >= self.rapid_confidence_threshold and rapid_cov["score"] >= self.rapid_coverage_threshold:
                selected = []
                fast_path_accepted = True
            else:
                selected = ["full"]
                missing = set(rapid_cov.get("missing") or [])
                if missing & {"mrp", "quantity", "inclusive_tax", "mfg_date"}: selected.append("bottom")
                if "generic_name" in missing: selected.append("top")
                escalation_reason = "rapid_first_recovery"
            if selected:
                t1 = time.perf_counter()
                with ThreadPoolExecutor(max_workers=min(2, len(selected))) as pool:
                    futures = {name: pool.submit(self._run_named_tesseract, source, name) for name in selected}
                    for name in selected:
                        tess_results[name] = futures[name].result()
                tess_elapsed = time.perf_counter() - t1

        else:
            # Production adaptive path: Tesseract first. The initial full-label PSM
            # 11 read is the low-latency path for clean, flat package labels.
            t0 = time.perf_counter()
            tess_results["full"] = self._run_named_tesseract(source, "full")
            tess_elapsed += time.perf_counter() - t0

            tess_text = tess_results["full"][0]
            tess_conf = tess_results["full"][1]
            tess_cov = self._coverage(tess_text, source_type=source_type) if tess_text else {"score": 0.0, "signals": {}, "missing": []}
            fast_path_accepted, gate_reasons = self._safe_to_skip_rapid(tess_text, tess_conf, tess_cov, context)

            # Before paying RapidOCR latency, try only cheap Tesseract zones that
            # directly correspond to still-missing evidence.
            if not fast_path_accepted:
                selected = self._select_tesseract_recovery_passes(tess_cov)
                if selected:
                    t1 = time.perf_counter()
                    with ThreadPoolExecutor(max_workers=min(2, len(selected))) as pool:
                        futures = {name: pool.submit(self._run_named_tesseract, source, name) for name in selected}
                        for name in selected:
                            tess_results[name] = futures[name].result()
                    tess_elapsed += time.perf_counter() - t1
                    tess_text = _dedupe_lines(*(tess_results[n][0] for n in ["full", "bottom", "top"] if n in tess_results))
                    tess_conf = self._average_confidence([tess_results[n][1] for n in tess_results])
                    tess_cov = self._coverage(tess_text, source_type=source_type) if tess_text else tess_cov
                    fast_path_accepted, gate_reasons = self._safe_to_skip_rapid(tess_text, tess_conf, tess_cov, context)

            if not fast_path_accepted:
                escalation_reason = "+".join(gate_reasons) if gate_reasons else "accuracy_guard"
                t2 = time.perf_counter()
                rapid_attempted = True
                rapid_text, rapid_conf = self._rapid_pass(source)
                rapid_elapsed = time.perf_counter() - t2

                # Accuracy guard: after merging RapidOCR, use raw PSM 3 whenever
                # the combined read remains materially incomplete OR a core anchor
                # is still missing. This intentionally favours accuracy over speed
                # on hard labels and when RapidOCR is unavailable/fails.
                provisional = _dedupe_lines(*(tess_results[n][0] for n in ["full", "bottom", "top"] if n in tess_results), rapid_text)
                post_cov = self._coverage(provisional, source_type=source_type) if provisional else {"score": 0.0, "signals": {}, "missing": []}
                post_signals = post_cov.get("signals") or {}
                core_after = ["mrp", "quantity", "company"]
                if context.get("evidence_scope") in {"complete", "complete_package", "all_panels", "complete_evidence"}:
                    core_after += ["address", "consumer_marker", "consumer_contact", "mfg_date"]
                missing_core_after = [name for name in core_after if not post_signals.get(name)]
                need_raw = (
                    not rapid_text
                    or bool(missing_core_after)
                    or post_cov.get("score", 0.0) < 0.78
                    or len(post_cov.get("missing") or []) >= 2
                )
                if self.accuracy_guard and need_raw:
                    t3 = time.perf_counter()
                    tess_results["raw"] = self._run_named_tesseract(source, "raw")
                    tess_elapsed += time.perf_counter() - t3
                    detail = ",".join(missing_core_after) if missing_core_after else "coverage"
                    escalation_reason += "+raw_accuracy_guard:" + detail

        ordered_names = [n for n in ["full", "bottom", "top", "raw"] if n in tess_results]
        tess_text = _dedupe_lines(*(tess_results[n][0] for n in ordered_names))
        merged = _dedupe_lines(tess_text, rapid_text)
        final_cov = self._coverage(merged, source_type=source_type) if merged else {"score": 0.0, "signals": {}, "missing": []}
        tess_conf = self._average_confidence([tess_results[n][1] for n in ordered_names])
        conf = self._average_confidence([tess_conf, rapid_conf]) or 0.0

        # Prefer a dedicated top-zone read when it ran. Otherwise use the first
        # lines from the strongest available OCR text as a product-title hint.
        top_text = tess_results.get("top", ("", None))[0]
        if not top_text:
            hint_source = tess_text or rapid_text
            if hint_source:
                top_text = "\n".join(hint_source.splitlines()[:8])

        codes = self._decode_codes(source)
        total_elapsed = time.perf_counter() - started
        route = (
            "tesseract_fast" if self.mode == "adaptive" and fast_path_accepted and not rapid_attempted
            else "tesseract_then_rapid" if self.mode == "adaptive" and rapid_text
            else "tesseract_recovery_rapid_unavailable" if self.mode == "adaptive" and rapid_attempted and not rapid_text
            else self.mode
        )
        return {
            "text": merged,
            "top_text": top_text,
            "ocr_confidence": conf,
            "ocr_confidence_by_engine": {
                "tesseract": tess_conf,
                "tesseract_passes": {name: tess_results[name][1] for name in ordered_names},
                "rapidocr": rapid_conf,
            },
            "engines_used": {
                "rapidocr": bool(rapid_text),
                "tesseract": any(bool(v[0]) for v in tess_results.values()),
                "tesseract_languages": self.languages,
                "strategy": self.mode,
                "route": route,
                "tesseract_passes": ordered_names,
                "fallback_triggered": bool(rapid_attempted) if self.mode == "adaptive" else bool(escalation_reason),
                "escalation_triggered": bool(rapid_attempted) if self.mode == "adaptive" else bool(escalation_reason),
                "rapidocr_attempted": bool(rapid_attempted),
                "fallback_reason": escalation_reason,
                "fast_path_accepted": bool(fast_path_accepted),
            },
            "ocr_routing": {
                "tesseract_fast_coverage": self._coverage(tess_results.get("full", ("", None))[0], source_type=source_type) if "full" in tess_results else None,
                "final_coverage": final_cov,
                "tesseract_fast_confidence_threshold": self.tesseract_fast_confidence_threshold,
                "tesseract_fast_coverage_threshold": self.tesseract_fast_coverage_threshold,
                "tesseract_min_chars": self.tesseract_min_chars,
                "accuracy_guard": self.accuracy_guard,
                "gate_reasons": gate_reasons,
            },
            "ocr_timing_ms": {
                "tesseract": round(tess_elapsed * 1000, 1),
                "rapidocr": round(rapid_elapsed * 1000, 1),
                "total": round(total_elapsed * 1000, 1),
            },
            "codes": codes,
        }


def extract_quantity(text: str) -> Dict[str, Any]:
    patterns = [
        rf"(?:Net\s*(?:Wt|Weight|Content|Vol|Volume|Qty|Quantity)|Contents?)\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*({UNIT_ALT})\b",
        rf"\b(\d+(?:\.\d+)?)\s*({UNIT_ALT})\b",
    ]
    for pat in patterns:
        m = re.search(pat, text or "", re.I)
        if not m:
            continue
        value, raw_unit = m.group(1), m.group(2)
        u = raw_unit.lower()
        bad = u if u in NON_STANDARD_UNIT_MAP else None
        norm = NON_STANDARD_UNIT_MAP.get(u) or STANDARD_UNIT_MAP.get(u, raw_unit)
        try:
            numeric = float(value)
        except Exception:
            numeric = None
        return {
            "display": f"{value} {norm}",
            "value": numeric,
            "unit_raw": raw_unit,
            "unit": norm,
            "unit_violation": bad,
            "evidence": _clean_line(m.group(0)),
        }
    return {"display": None, "value": None, "unit_raw": None, "unit": None, "unit_violation": None, "evidence": None}


def _extract_role(text: str) -> Optional[str]:
    m = ROLE_MARKER.search(text or "")
    if not m: return None
    word = m.group(0).lower()
    if "import" in word: return "importer"
    if "pack" in word: return "packer"
    if "market" in word: return "marketer"
    if "distribut" in word: return "distributor"
    return "manufacturer"


def _candidate_block(text: str, marker: re.Pattern, radius: int = 3) -> Optional[str]:
    lines = [_clean_line(x) for x in (text or "").splitlines() if _clean_line(x)]
    for i, line in enumerate(lines):
        if marker.search(line):
            return "\n".join(lines[i:min(len(lines), i + radius + 1)])
    return None


def extract_address(text: str, company: Optional[str] = None) -> Dict[str, Any]:
    lines = [_clean_line(x) for x in (text or "").splitlines() if _clean_line(x)]
    candidates: List[str] = []
    for i, line in enumerate(lines):
        if ROLE_MARKER.search(line) or (company and company.lower() in line.lower()):
            block_parts = [line]
            for nxt in lines[i + 1:i + 4]:
                if DECLARATION_STOP.search(nxt) and not ADDRESS_HINTS.search(nxt):
                    break
                block_parts.append(nxt)
            candidates.append(" ".join(block_parts))
    # Generic fallback: lines containing a PIN and address hint.
    for i, line in enumerate(lines):
        if PIN_RE.search(line) and (ADDRESS_HINTS.search(line) or len(line) > 18):
            candidates.append(" ".join(lines[max(0, i-1):min(len(lines), i+2)]))
    best = None
    best_score = -1
    for c in candidates:
        pin = bool(PIN_RE.search(c))
        hints = len(ADDRESS_HINTS.findall(c))
        score = (4 if pin else 0) + min(hints, 4) + (1 if len(c) >= 25 else 0)
        if score > best_score:
            best_score, best = score, c
    complete = bool(best and (PIN_RE.search(best) or len(ADDRESS_HINTS.findall(best)) >= 2) and len(best) >= 18)
    return {"address": best, "complete": complete, "pin": (PIN_RE.search(best).group(0) if best and PIN_RE.search(best) else None)}


def extract_consumer_care(text: str, responsible_address: Optional[str] = None) -> Dict[str, Any]:
    block = _candidate_block(text, CARE_MARKER, radius=5)
    search_space = block or ""
    phone = PHONE_RE.search(search_space)
    email = EMAIL_RE.search(search_space)
    # If the care marker exists, allow a nearby/full-label contact to be considered candidate evidence,
    # but keep it reviewable unless a block association is clear.
    if block and not phone:
        phone = PHONE_RE.search(text or "")
    if block and not email:
        email = EMAIL_RE.search(text or "")
    care_address = None
    if block:
        addr = extract_address(block)
        if addr.get("complete"):
            care_address = addr.get("address")
        elif responsible_address and re.search(r"(?:same\s+as\s+above|address\s+as\s+above|at\s+above\s+address)", block, re.I):
            care_address = responsible_address
    # FAQs permit referring to address information elsewhere; when a care block clearly exists,
    # the responsible address can therefore be treated as address evidence, but surfaced transparently.
    if block and not care_address and responsible_address:
        care_address = responsible_address
    return {
        "marker_found": bool(block),
        "block": block,
        "phone": phone.group(0) if phone else None,
        "email": email.group(0) if email else None,
        "address": care_address,
        "name_or_office": (_clean_line(block.splitlines()[0]) if block else None),
    }


def extract_country_of_origin(text: str) -> Optional[str]:
    m = COO_RE.search(text or "")
    if not m: return None
    val = _clean_line(m.group(1))
    # Trim declaration text that OCR merged onto the same line.
    val = re.split(r"\b(?:MRP|Net|Mfg|Batch|Customer|Consumer)\b", val, maxsplit=1, flags=re.I)[0].strip(" ,;.-")
    return val[:60] or None


def extract_generic_name(text: str) -> Optional[str]:
    m = GENERIC_RE.search(text or "")
    if not m: return None
    val = _clean_line(m.group(1))
    val = re.split(r"\b(?:Net|MRP|Mfg|Batch|Country)\b", val, maxsplit=1, flags=re.I)[0].strip(" ,;.-")
    return val[:80] or None


def extract_unit_price(text: str) -> Optional[Dict[str, Any]]:
    for pattern in UNIT_PRICE_PATTERNS:
        m = pattern.search(text or "")
        if not m:
            continue
        try:
            value = float(m.group(1).replace(",", "."))
        except Exception:
            value = None
        raw_unit = (m.group(2) or "").strip()
        unit_key = raw_unit.lower()
        norm = STANDARD_UNIT_MAP.get(unit_key) or NON_STANDARD_UNIT_MAP.get(unit_key) or raw_unit
        return {"value": value, "unit": norm, "unit_raw": raw_unit, "evidence": _clean_line(m.group(0))}
    return None


def expected_unit_sale_price(quantity: Dict[str, Any], mrp: Optional[float]) -> Dict[str, Any]:
    """Calculate the Rule 6(11) comparison basis when quantity and MRP are parseable.

    This does not decide every statutory exemption. It only resolves the ordinary
    mass/volume/length/number cases and the explicit proviso where retail sale
    price equals the unit sale price.
    """
    if mrp is None or quantity.get("value") is None or not quantity.get("unit"):
        return {"known": False, "required": None, "reason": "MRP or net quantity is unavailable"}
    try:
        price = float(mrp)
        value = float(quantity["value"])
    except Exception:
        return {"known": False, "required": None, "reason": "MRP or quantity is not numeric"}
    if price < 0 or value <= 0:
        return {"known": False, "required": None, "reason": "MRP/quantity is not positive"}

    unit = quantity["unit"]
    # Department FAQ Q43: retail packs of 10 g / 10 ml or less are exempt from
    # unit-sale-price declaration, aligned with the small-pack Rule 26 exemption.
    grams_equiv = value / 1000.0 if unit == "mg" else (value if unit == "g" else None)
    ml_equiv = value if unit == "ml" else None
    if (grams_equiv is not None and grams_equiv <= 10.0) or (ml_equiv is not None and ml_equiv <= 10.0):
        return {
            "known": True,
            "required": False,
            "reason": "Small-pack exemption (10 g / 10 ml or less)",
            "retail_sale_price": round(price, 2),
            "small_pack_exemption": True,
        }
    basis_unit = None
    denominator = None
    if unit == "mg":
        grams = value / 1000.0
        basis_unit, denominator = "g", grams
    elif unit == "g":
        grams = value
        if grams < 1000:
            basis_unit, denominator = "g", grams
        else:
            basis_unit, denominator = "kg", grams / 1000.0
    elif unit == "kg":
        grams = value * 1000.0
        if grams < 1000:
            basis_unit, denominator = "g", grams
        else:
            basis_unit, denominator = "kg", grams / 1000.0
    elif unit == "ml":
        ml = value
        if ml < 1000:
            basis_unit, denominator = "ml", ml
        else:
            basis_unit, denominator = "L", ml / 1000.0
    elif unit == "L":
        ml = value * 1000.0
        if ml < 1000:
            basis_unit, denominator = "ml", ml
        else:
            basis_unit, denominator = "L", ml / 1000.0
    elif unit == "cm":
        cm = value
        if cm < 100:
            basis_unit, denominator = "cm", cm
        else:
            basis_unit, denominator = "m", cm / 100.0
    elif unit == "m":
        cm = value * 100.0
        if cm < 100:
            basis_unit, denominator = "cm", cm
        else:
            basis_unit, denominator = "m", cm / 100.0
    elif unit == "pcs":
        basis_unit, denominator = "pcs", value

    if not basis_unit or not denominator:
        return {"known": False, "required": None, "reason": f"Unit '{unit}' is outside the automatic unit-price calculator"}
    expected = round(price / denominator + 1e-12, 2)
    exempt_equal = abs(expected - round(price, 2)) < 0.005
    return {
        "known": True,
        "required": not exempt_equal,
        "basis_unit": basis_unit,
        "expected_value": expected,
        "retail_sale_price": round(price, 2),
        "proviso_equal_to_mrp": exempt_equal,
    }


def validate_unit_sale_price(quantity: Dict[str, Any], mrp: Optional[float], detected: Optional[Dict[str, Any]], evidence_scope: str) -> Dict[str, Any]:
    calc = expected_unit_sale_price(quantity, mrp)
    if not calc.get("known"):
        return {"status": "needs_review", "value": detected, "calculation": calc, "note": "Unit sale price applicability could not be resolved automatically."}
    if not calc.get("required"):
        if calc.get("small_pack_exemption"):
            note = "Unit sale price is not required for this screening case because the declared retail pack is 10 g / 10 ml or less."
        else:
            note = "Rule 6(11) proviso applies because the calculated unit sale price equals the retail sale price."
        return {"status": "not_applicable", "value": detected, "calculation": calc, "note": note}
    if not detected or detected.get("value") is None:
        return {"status": _status_for_missing(evidence_scope), "value": detected, "calculation": calc, "note": "Unit sale price was not detected although the ordinary Rule 6(11) calculation indicates it is required."}

    unit_alias = {"pc": "pcs", "piece": "pcs", "number": "pcs", "unit": "pcs", "liter": "L", "litre": "L", "l": "L", "meter": "m", "metre": "m"}
    det_unit = unit_alias.get(str(detected.get("unit") or "").lower(), detected.get("unit"))
    expected_unit = calc.get("basis_unit")
    unit_ok = det_unit == expected_unit
    value_ok = abs(float(detected["value"]) - float(calc["expected_value"])) <= 0.02
    if unit_ok and value_ok:
        return {"status": "compliant", "value": detected, "calculation": calc, "note": "Detected unit sale price matches the Rule 6(11) screening calculation to two decimals."}
    status = "non_compliant" if evidence_scope in {"complete_package", "complete_listing"} else "needs_review"
    reason = []
    if not unit_ok:
        reason.append(f"basis unit '{det_unit}' does not match expected '{expected_unit}'")
    if not value_ok:
        reason.append(f"value {detected.get('value')} does not match expected {calc.get('expected_value')}")
    return {"status": status, "value": detected, "calculation": calc, "note": "Unit sale price needs verification: " + "; ".join(reason)}


def extract_exp_date_no_magic(ex: Extractor, text: str, mfg_date: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    exp = ex._extract_date(text, Patterns.EXP)
    if exp:
        return exp
    for p in Patterns.SHELF_LIFE:
        m = re.search(p, text or "", re.I)
        if not m: continue
        try: months = int(m.group(1))
        except Exception: continue
        if not (1 <= months <= 240):
            return {"month": None, "year": None, "display": f"OCR read shelf life as {months} months", "needs_review": True}
        stmt = f"Use before {months} months of manufacturing"
        if mfg_date and mfg_date.get("year") and mfg_date.get("month"):
            total = int(mfg_date["year"]) * 12 + int(mfg_date["month"]) - 1 + months
            y, mo = divmod(total, 12)
            mo += 1
            return {"month": mo, "year": y, "month_name": mo, "shelf_life_statement": stmt, "inferred_from_shelf_life": f"{months} Months from Mfg Date", "display": f"{months} Months from Mfg ({mo:02d}/{y})"}
        return {"month": None, "year": None, "shelf_life_statement": stmt, "inferred_from_shelf_life": f"{months} Months from Mfg Date", "display": stmt}
    return None


def expiry_is_past(exp: Dict[str, Any], today: Optional[date] = None) -> bool:
    if not exp or not exp.get("year") or not exp.get("month"):
        return False
    today = today or date.today()
    y, m = int(exp["year"]), int(exp["month"])
    if exp.get("day"):
        try: expiry_day = date(y, m, int(exp["day"]))
        except ValueError: return False
    else:
        # Month/year declarations remain current through the last calendar day of that month.
        expiry_day = date(y, m, calendar.monthrange(y, m)[1])
    return expiry_day < today


def validate_font(context: Dict[str, Any], source_type: str) -> Dict[str, Any]:
    category = (context.get("category") or "general").strip().lower()
    if source_type == "ecommerce":
        return _issue_check("font_size", "not_applicable", "Physical Rule 7 character-height measurement is not assessed from an e-commerce listing.", issues=[])
    h = context.get("font_height_mm")
    area = context.get("pdp_area_cm2")
    width = context.get("font_width_mm")
    moulded = bool(context.get("moulded_text"))
    try: h = float(h) if h not in (None, "") else None
    except Exception: h = None
    try: area = float(area) if area not in (None, "") else None
    except Exception: area = None
    try: width = float(width) if width not in (None, "") else None
    except Exception: width = None
    if h is None or area is None:
        return _issue_check(
            "font_size", "needs_review",
            "Calibrated character height and principal display panel area were not supplied; photo pixels alone cannot establish statutory millimetres.",
            issues=["Physical measurement required before a certified Rule 7 pass/fail."],
        )
    required = required_font_height_mm(area, moulded=moulded)
    # Some commodity categories are also governed by sector-specific labelling laws.
    # We still show the baseline measurement, but do not turn it into a definitive green/red
    # legal conclusion without category-specific rule review.
    category_review = category in {"cosmetics", "medical_device", "electronics", "other"}
    issues = []
    if h + 1e-9 < required:
        issues.append(f"Measured character height {h:g} mm is below the {required:g} mm baseline for PDP area {area:g} cm².")
    if width is not None and width + 1e-9 < h / 3.0:
        issues.append(f"Measured character width {width:g} mm is less than one-third of height {h:g} mm (subject to Rule 7 character exceptions).")
    status = "non_compliant" if issues else ("needs_review" if category_review else "compliant")
    if category_review and not issues:
        issues.append(f"Measured size meets the baseline table, but category '{category}' may have additional or overriding character-size provisions; verify the applicable sector rule.")
    return _issue_check(
        "font_size", status,
        f"Measured {h:g} mm; screening threshold {required:g} mm for PDP area {area:g} cm²{' (moulded/formed text)' if moulded else ''}.",
        evidence={"font_height_mm": h, "font_width_mm": width, "pdp_area_cm2": area, "moulded_text": moulded, "required_height_mm": required},
        issues=issues,
    )


class ComplianceEngine:
    def __init__(self):
        self.ocr = HybridOCR()
        self.ex = Extractor()

    def analyze_images(self, images: Sequence[bytes], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        context = dict(context or {})
        reads = [self.ocr.read(b, context=context) for b in images]
        raw_text = _dedupe_lines(*(r["text"] for r in reads))
        top_text = _dedupe_lines(*(r.get("top_text", "") for r in reads))
        confs = [float(r.get("ocr_confidence", 0)) for r in reads if r.get("ocr_confidence") is not None]
        codes = []
        for r in reads: codes.extend(r.get("codes") or [])
        result = self.analyze_text(raw_text, context={**context, "source_type": context.get("source_type", "physical")}, top_text=top_text)
        result["ocr_confidence"] = round(sum(confs) / len(confs), 1) if confs else 0.0
        result["confidence"] = int(round(result["ocr_confidence"]))  # compatibility only
        result["ocr_confidence_by_panel"] = [r.get("ocr_confidence") for r in reads]
        result["engines_used"] = {
            "rapidocr": any(r.get("engines_used", {}).get("rapidocr") for r in reads),
            "tesseract": any(r.get("engines_used", {}).get("tesseract") for r in reads),
            "tesseract_languages": self.ocr.languages,
            "strategy": self.ocr.mode,
            "fallback_panels": sum(1 for r in reads if r.get("engines_used", {}).get("fallback_triggered")),
            "escalation_panels": sum(1 for r in reads if r.get("engines_used", {}).get("escalation_triggered")),
            "fast_path_panels": sum(1 for r in reads if r.get("engines_used", {}).get("fast_path_accepted")),
            "routes_by_panel": [r.get("engines_used", {}).get("route") for r in reads],
            "tesseract_passes_by_panel": [r.get("engines_used", {}).get("tesseract_passes", []) for r in reads],
        }
        result["ocr_routing_by_panel"] = [r.get("ocr_routing") for r in reads]
        result["ocr_timing_ms_by_panel"] = [r.get("ocr_timing_ms") for r in reads]
        result["ocr_timing_ms"] = round(sum(float((r.get("ocr_timing_ms") or {}).get("total", 0.0)) for r in reads), 1)
        # dedupe codes
        seen = set(); unique_codes = []
        for c in codes:
            key = (c.get("type"), c.get("value"))
            if key not in seen: seen.add(key); unique_codes.append(c)
        result["codes"] = unique_codes
        return result

    def analyze_text(self, text: str, context: Optional[Dict[str, Any]] = None, top_text: str = "") -> Dict[str, Any]:
        context = dict(context or {})
        source_type = context.get("source_type") or "ecommerce"
        evidence_scope = context.get("evidence_scope") or ("partial" if source_type == "physical" else "complete_listing")
        category = (context.get("category") or "general").strip().lower()
        if category not in CATEGORY_PROFILES:
            category = "other"
        category_profile = CATEGORY_PROFILES[category]
        text = text or ""
        qty = extract_quantity(text)
        mfg = self.ex.extract_mfg_date(text)
        exp = extract_exp_date_no_magic(self.ex, text, mfg)
        company = self.ex.extract_company(text)
        addr = extract_address(text, company)
        care = extract_consumer_care(text, addr.get("address") if addr.get("complete") else None)
        country = extract_country_of_origin(text)
        generic = extract_generic_name(text)
        role = _extract_role(text)
        imported_detected = role == "importer" or bool(re.search(r"\bimport(?:ed|er)\b", text, re.I))
        import_context = context.get("imported")
        if import_context in ("true", "1", 1, True): imported = True
        elif import_context in ("false", "0", 0, False): imported = False
        else: imported = imported_detected
        mrp = self.ex.extract_mrp(text)
        inclusive = self.ex.check_inclusive_tax(text)
        fssai = self.ex.extract_fssai(text)
        product_name = self.ex.extract_product_name(text, top_text=top_text)
        unit_price = extract_unit_price(text)

        checks: Dict[str, Dict[str, Any]] = {}

        # 1 - identity + responsible entity
        r_issues, r_sub = [], []
        missing_status = _status_for_missing(evidence_scope)
        if company:
            r_sub.append({"name": "Responsible entity name", "status": "compliant", "value": company})
        else:
            r_issues.append("Manufacturer/packer/importer name not detected.")
            r_sub.append({"name": "Responsible entity name", "status": missing_status, "value": None})
        if addr.get("complete"):
            r_sub.append({"name": "Locatable postal address", "status": "compliant", "value": addr.get("address")})
        else:
            r_issues.append("A complete locatable postal address was not established from the supplied evidence.")
            r_sub.append({"name": "Locatable postal address", "status": missing_status, "value": addr.get("address")})
        if generic:
            r_sub.append({"name": "Common/generic commodity name", "status": "compliant", "value": generic})
        else:
            # Keep within the grouped model. It is a mandatory Rule 6 declaration but OCR may infer product title.
            r_issues.append("Common/generic commodity name was not explicitly detected.")
            r_sub.append({"name": "Common/generic commodity name", "status": missing_status, "value": None})
        if imported:
            if country:
                r_sub.append({"name": "Country of origin", "status": "compliant", "value": country})
            else:
                r_issues.append("Imported-product context detected/selected but country of origin was not found.")
                r_sub.append({"name": "Country of origin", "status": missing_status, "value": None})
        if category == "food":
            # DoCA FAQ guidance treats MRP, net quantity and consumer care as the LMPC
            # declarations for food governed by FSSAI. Keep identity evidence visible,
            # but do not turn missing FSSAI-governed fields into an LMPC violation.
            for sub in r_sub:
                sub["status"] = "not_applicable"
                sub["note"] = "Cross-law review for FSSAI-governed food; not used as an LMPC pass/fail criterion in this profile."
            r_status = "not_applicable"
            r_issues = []
        elif r_issues:
            r_status = "non_compliant" if any(s.get("status") == "non_compliant" for s in r_sub) else "needs_review"
        else:
            r_status = "compliant"
        checks["responsible_entity"] = _issue_check(
            "responsible_entity", r_status,
            f"{role.title() if role else 'Responsible entity'}: {company or 'not detected'}" + (f"; address candidate: {addr.get('address')}" if addr.get("address") else ""),
            evidence={"company": company, "role": role, "address": addr, "generic_name": generic, "imported": imported, "country_of_origin": country},
            issues=r_issues, subchecks=r_sub,
        )

        # 2 - quantity
        q_issues, q_status = [], "compliant"
        if qty.get("display") is None:
            q_status = missing_status; q_issues.append("Net quantity was not detected.")
        else:
            if qty.get("value") is not None and qty["value"] <= 0:
                q_status = "non_compliant"; q_issues.append("Net quantity must be positive.")
            if qty.get("unit_violation"):
                q_status = "non_compliant"; q_issues.append(f"Non-standard unit '{qty['unit_raw']}' detected; use the recognised declaration form '{qty['unit']}'.")
        checks["net_quantity"] = _issue_check("net_quantity", q_status, qty.get("display") or "Net quantity not detected.", evidence=qty, issues=q_issues)

        # 3 - MRP + Rule 6(11) unit sale price
        p_issues, p_sub = [], []
        if mrp is None:
            mrp_status = missing_status
            p_issues.append("MRP was not detected.")
        else:
            mrp_status = "compliant" if float(mrp) >= 0 else "non_compliant"
            if float(mrp) < 0:
                p_issues.append("Detected MRP is negative and requires correction/review.")
        p_sub.append({"name": "Retail sale price / MRP", "status": mrp_status, "value": (f"₹{float(mrp):.2f}" if mrp is not None else None)})

        if mrp is None:
            inclusive_status = missing_status
        else:
            inclusive_status = "compliant" if inclusive else _status_for_missing(evidence_scope)
            if not inclusive:
                p_issues.append("MRP was detected without the inclusive-of-all-taxes wording in the supplied evidence; partial evidence is not treated as proof that the wording is absent from the package.")
        p_sub.append({"name": "Inclusive of all taxes", "status": inclusive_status, "value": bool(inclusive)})

        usp = validate_unit_sale_price(qty, mrp, unit_price, evidence_scope)
        p_sub.append({"name": "Unit sale price", "status": usp["status"], "value": usp.get("value"), "note": usp.get("note"), "calculation": usp.get("calculation")})
        if usp["status"] in {"non_compliant", "needs_review"}:
            p_issues.append(usp.get("note") or "Unit sale price requires review.")

        p_statuses = [x["status"] for x in p_sub if x["status"] != "not_applicable"]
        p_status = "non_compliant" if "non_compliant" in p_statuses else ("needs_review" if "needs_review" in p_statuses else "compliant")
        checks["mrp"] = _issue_check(
            "mrp", p_status,
            (f"₹{float(mrp):.2f}" if mrp is not None else "MRP not detected") + ("; inclusive-of-all-taxes wording detected" if inclusive else "; inclusive-tax wording not detected"),
            evidence={"mrp": mrp, "inclusive_tax": inclusive, "unit_sale_price": unit_price, "unit_sale_price_screen": usp},
            issues=p_issues, subchecks=p_sub,
        )

        # 4 - month/year plus shelf-life evidence. Rule 6(10) excludes only the
        # month/year display from e-commerce; an applicable expiry/use-by declaration
        # must not be accidentally hidden by marking the entire group N/A.
        d_issues, d_sub = [], []
        if category == "food":
            mfg_status = "not_applicable"
            d_sub.append({"name": "Manufacture/packing month-year", "status": mfg_status, "value": mfg, "note": "Cross-law/FSSAI labelling review; not used as an LMPC pass/fail criterion in the food profile."})
        elif source_type == "ecommerce":
            mfg_status = "not_applicable"
            d_sub.append({"name": "Manufacture/packing month-year", "status": mfg_status, "value": None, "note": "Excluded from the Rule 6(10) e-commerce network display requirement."})
        elif mfg is None:
            if category in {"food", "cosmetics", "medical_device", "electronics", "other"}:
                mfg_status = "needs_review"
                d_issues.append(f"Manufacture/packing/import date was not detected; category '{category}' may have specific provisions/exemptions that should be reviewed before a definitive finding.")
            else:
                mfg_status = missing_status
                d_issues.append("Required manufacture/packing/import month-year was not detected in the supplied package evidence.")
            d_sub.append({"name": "Manufacture/packing month-year", "status": mfg_status, "value": None})
        else:
            mfg_status = "compliant"
            d_sub.append({"name": "Manufacture/packing month-year", "status": "compliant", "value": mfg.get("display") or f"{mfg.get('month')}/{mfg.get('year')}"})

        if category == "food":
            shelf_status = "not_applicable"
            d_sub.append({"name": "Best-before / use-by when applicable", "status": shelf_status, "value": exp, "note": "Surfaced for FSSAI/cross-law review; not used as an LMPC pass/fail criterion in the food profile."})
        elif exp and exp.get("needs_review"):
            shelf_status = "needs_review"
            d_issues.append(exp.get("display") or "Shelf-life OCR needs review.")
            d_sub.append({"name": "Best-before / use-by when applicable", "status": shelf_status, "value": exp, "note": "Commodity-specific applicability must be confirmed."})
        elif exp and expiry_is_past(exp):
            shelf_status = "non_compliant"
            exp_display = exp.get("display") or f"{exp.get('month')}/{exp.get('year')}"
            d_issues.append(f"Detected expiry/use-by appears to have passed: {exp_display}.")
            d_sub.append({"name": "Best-before / use-by when applicable", "status": shelf_status, "value": exp, "note": "Commodity-specific applicability must be confirmed."})
        elif exp:
            shelf_status = "needs_review"
            d_issues.append("Shelf-life evidence was detected, but automatic LMPC applicability is not assumed for this category; verify the governing commodity rule.")
            d_sub.append({"name": "Best-before / use-by when applicable", "status": shelf_status, "value": exp, "note": "Commodity-specific applicability must be confirmed."})
        else:
            shelf_status = "not_applicable"
            d_sub.append({"name": "Best-before / use-by when applicable", "status": shelf_status, "value": exp, "note": "No automatic LMPC shelf-life applicability assumed for this category."})

        if category != "food" and mfg and exp and mfg.get("year") and mfg.get("month") and exp.get("year") and exp.get("month"):
            if (int(mfg["year"]), int(mfg["month"])) > (int(exp["year"]), int(exp["month"])):
                shelf_status = "non_compliant"
                d_sub[-1]["status"] = "non_compliant"
                d_issues.append("Detected manufacturing/packing date is after the detected expiry date.")

        d_statuses = [x["status"] for x in d_sub if x["status"] != "not_applicable"]
        d_status = "non_compliant" if "non_compliant" in d_statuses else ("needs_review" if "needs_review" in d_statuses else ("compliant" if d_statuses else "not_applicable"))
        if category == "food":
            d_summary = "Date/shelf-life evidence is surfaced for FSSAI/cross-law review and is not used as an LMPC pass/fail criterion in the food profile."
        elif mfg:
            d_summary = "Date evidence detected: " + (mfg.get("display") or f"{mfg.get('month')}/{mfg.get('year')}")
        elif source_type == "ecommerce":
            d_summary = "Manufacture/packing month-year is excluded from the e-commerce display screen under Rule 6(10)."
        else:
            d_summary = "Manufacture/packing/import month-year not detected."
        if exp:
            d_summary += "; shelf-life evidence: " + str(exp.get("display") or f"{exp.get('month')}/{exp.get('year')}")
        checks["date"] = _issue_check("date", d_status, d_summary, evidence={"mfg_date": mfg, "exp_date": exp}, issues=d_issues, subchecks=d_sub)

        # 5 - consumer care
        c_issues, c_sub = [], []
        required_parts = {
            "Consumer-care marker/name or office": care.get("name_or_office"),
            "Postal address": care.get("address"),
            "Telephone": care.get("phone"),
            "E-mail": care.get("email"),
        }
        for name, value in required_parts.items():
            st = "compliant" if value else missing_status
            c_sub.append({"name": name, "status": st, "value": value})
            if not value: c_issues.append(f"{name} was not established from the supplied evidence.")
        c_status = "compliant" if not c_issues else ("non_compliant" if any(x["status"] == "non_compliant" for x in c_sub) else "needs_review")
        checks["consumer_care"] = _issue_check("consumer_care", c_status, care.get("block") or "Consumer-care block not established.", evidence=care, issues=c_issues, subchecks=c_sub)

        # 6 - calibrated font
        checks["font_size"] = validate_font(context, source_type)

        relevant = [c for c in checks.values() if c.get("status") != "not_applicable"]
        violations = []
        review_items = []
        for c in relevant:
            if c["status"] == "non_compliant":
                violations.extend([f"{c['title']}: {x}" for x in (c.get("issues") or [c.get("summary")])])
            elif c["status"] == "needs_review":
                review_items.extend([f"{c['title']}: {x}" for x in (c.get("issues") or [c.get("summary")])])
        overall = "non_compliant" if violations else ("needs_review" if review_items else "compliant")

        extraction_fields = [company, addr.get("complete"), qty.get("display"), mrp, inclusive, care.get("phone"), care.get("email")]
        # Physical date counts; e-commerce date is N/A rather than absent.
        if source_type == "physical": extraction_fields.append(mfg)
        completeness = round(100 * sum(1 for x in extraction_fields if x) / max(1, len(extraction_fields)))

        return {
            "product_name": product_name,
            "mrp": mrp,
            "quantity": qty.get("display"),
            "quantity_details": qty,
            "mfg_date": mfg,
            "exp_date": exp,
            "company": company,
            "manufacturer": {"name": company, "role": role, "address": addr.get("address"), "address_complete": addr.get("complete"), "pin": addr.get("pin")},
            "country_of_origin": country,
            "generic_name": generic,
            "fssai": fssai,
            "consumer_care": care,
            "inclusive_tax": inclusive,
            "unit_violation": qty.get("unit_violation"),
            "unit_sale_price": unit_price,
            "raw_text": text,
            "lines": [_clean_line(x) for x in text.splitlines() if _clean_line(x)],
            "source_type": source_type,
            "evidence_scope": evidence_scope,
            "category": category,
            "category_profile": category_profile,
            "checks": checks,
            "compliance_status": overall,
            "lmpc": {
                "is_compliant": overall == "compliant",
                "status": overall,
                "violations": violations,
                "needs_review": review_items,
            },
            # Backward-compatible alias for older frontend integrations.
            "pcr_2011": {
                "is_compliant": overall == "compliant",
                "status": overall,
                "violations": violations,
                "needs_review": review_items,
            },
            "extraction_completeness": completeness,
            "ruleset_id": RULESET_ID,
            "engine_version": "2.4.0",
            "ocr_confidence": None if source_type == "ecommerce" else 0.0,
            "confidence": 0,
            "engines_used": {"text_input": source_type == "ecommerce"},
        }
