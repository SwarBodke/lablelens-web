"""Benchmark LableLens adaptive OCR against forced full ensemble on one image.

Usage:
    python benchmark_ocr.py /path/to/product.jpg

The adaptive route is production behavior. Ensemble is the accuracy reference: it
forces RapidOCR plus all Tesseract passes. The script reports speed and key-field
parity so latency improvements are not accepted silently when extraction changes.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from compliance_engine import ComplianceEngine


def summarize(mode: str, result: dict, elapsed: float) -> None:
    print(f"\n[{mode}]")
    print(f"wall_time_s: {elapsed:.3f}")
    print(f"ocr_timing_ms: {result.get('ocr_timing_ms')}")
    print(f"ocr_confidence: {result.get('ocr_confidence')}")
    print(f"extraction_completeness: {result.get('extraction_completeness')}")
    print(f"engines_used: {result.get('engines_used')}")
    print(f"mrp: {result.get('mrp')}")
    print(f"quantity: {result.get('quantity')}")
    print(f"mfg_date: {result.get('mfg_date')}")
    print(f"company: {result.get('company')}")
    care = result.get('consumer_care') or {}
    print(f"consumer_phone: {care.get('phone')}")
    print(f"consumer_email: {care.get('email')}")


def _stable(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        # Keep only semantic values; evidence strings often differ by OCR route.
        keep = {k: value.get(k) for k in ("value", "unit", "display", "month", "year", "phone", "email") if k in value}
        return json.dumps(keep, sort_keys=True, default=str).lower().strip()
    return " ".join(str(value).lower().split())


def parity(adaptive: dict, ensemble: dict) -> list[str]:
    pairs = {
        "mrp": (adaptive.get("mrp"), ensemble.get("mrp")),
        "quantity": (adaptive.get("quantity"), ensemble.get("quantity")),
        "mfg_date": (adaptive.get("mfg_date"), ensemble.get("mfg_date")),
        "company": (adaptive.get("company"), ensemble.get("company")),
        "consumer_phone": ((adaptive.get("consumer_care") or {}).get("phone"), (ensemble.get("consumer_care") or {}).get("phone")),
        "consumer_email": ((adaptive.get("consumer_care") or {}).get("email"), (ensemble.get("consumer_care") or {}).get("email")),
    }
    mismatches = []
    for name, (a, e) in pairs.items():
        sa, se = _stable(a), _stable(e)
        # Ensemble is the reference only when it actually recovered a value.
        if se and sa != se:
            mismatches.append(f"{name}: adaptive={a!r} | ensemble={e!r}")
    return mismatches


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    args = ap.parse_args()
    p = Path(args.image)
    if not p.is_file():
        raise SystemExit(f"Image not found: {p}")
    blob = p.read_bytes()
    engine = ComplianceEngine()
    context = {"source_type": "physical", "evidence_scope": "partial", "category": "general"}

    # Warm both native engines once so model/process startup does not distort the
    # measured production-vs-reference comparison.
    engine.ocr._get_rapid()
    engine.ocr.mode = "tesseract_only"
    engine.ocr._run_named_tesseract(blob, "full")

    results = {}
    for mode in ("adaptive", "ensemble"):
        engine.ocr.mode = mode
        t0 = time.perf_counter()
        result = engine.analyze_images([blob], context=context)
        elapsed = time.perf_counter() - t0
        results[mode] = (result, elapsed)
        summarize(mode, result, elapsed)

    a_result, a = results["adaptive"]
    e_result, e = results["ensemble"]
    if e > 0:
        print(f"\nspeedup: {e/a:.2f}x" if a > 0 else "\nspeedup: n/a")
        print(f"time_saved_percent: {(e-a)/e*100:.1f}%")

    mismatches = parity(a_result, e_result)
    if mismatches:
        print("\nACCURACY PARITY: FAIL")
        for item in mismatches:
            print(" - " + item)
        print("Adaptive routing changed a key field recovered by the full ensemble. Keep/escalate OCR for this label and tune the gate before using the fast path broadly.")
        return 2

    print("\nACCURACY PARITY: PASS")
    print("Adaptive routing preserved every key value recovered by the forced ensemble on this image.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
