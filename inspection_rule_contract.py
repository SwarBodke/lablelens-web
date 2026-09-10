"""Six-group Legal Metrology result contract for LableLens.

This module sits *after* OCR and the compliance engine.  It does not perform
image processing or OCR.  Its job is to guarantee that every returned
inspection has the same six screening groups, conservative status semantics,
and an overall result that cannot contradict an individual check.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable

from ruleset import CHECKS, RULESET_ID

CHECK_ORDER = (
    "responsible_entity",
    "net_quantity",
    "mrp",
    "date",
    "consumer_care",
    "font_size",
)

_ALLOWED = {"compliant", "non_compliant", "needs_review", "not_applicable"}
_DISPLAY = {
    "compliant": "PASS",
    "non_compliant": "FAIL",
    "needs_review": "REVIEW",
    "not_applicable": "N/A",
}


def _status(value: Any) -> str:
    value = str(value or "needs_review").strip().lower()
    return value if value in _ALLOWED else "needs_review"


def _collect_issue_lines(checks: Iterable[Dict[str, Any]], target_status: str) -> list[str]:
    out: list[str] = []
    for check in checks:
        if check.get("status") != target_status:
            continue
        title = check.get("title") or "Compliance check"
        issues = check.get("issues") or []
        if not issues:
            summary = check.get("summary")
            if summary:
                issues = [summary]
        for issue in issues:
            line = f"{title}: {issue}"
            if line not in out:
                out.append(line)
    return out


def finalize_six_group_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a compliance result to the six-group inspection contract.

    The OCR transcription, confidence, engine route and extracted evidence are
    preserved exactly as supplied.  Missing groups are never guessed as a pass;
    they become REVIEW so that incomplete integration cannot yield a false green
    result.
    """
    if not isinstance(result, dict):
        return result

    raw_checks = result.get("checks") if isinstance(result.get("checks"), dict) else {}
    normalized: Dict[str, Dict[str, Any]] = {}

    for key in CHECK_ORDER:
        meta = CHECKS[key]
        original = raw_checks.get(key)
        if not isinstance(original, dict):
            original = {
                "id": key,
                "number": meta["number"],
                "title": meta["title"],
                "status": "needs_review",
                "summary": "This mandatory screening group was not returned by the rule engine and requires review.",
                "issues": ["Screening group result unavailable."],
                "subchecks": [],
                "rule": meta["rules"],
            }
        else:
            original = deepcopy(original)

        st = _status(original.get("status"))
        original["id"] = key
        original["number"] = meta["number"]
        original["title"] = meta["title"]
        original["rule"] = original.get("rule") or meta["rules"]
        original["status"] = st
        original["display_status"] = _DISPLAY[st]
        original.setdefault("issues", [])
        original.setdefault("subchecks", [])

        subs = []
        for sub in original.get("subchecks") or []:
            if not isinstance(sub, dict):
                continue
            sub = deepcopy(sub)
            sub_st = _status(sub.get("status"))
            sub["status"] = sub_st
            sub["display_status"] = _DISPLAY[sub_st]
            subs.append(sub)
        original["subchecks"] = subs
        normalized[key] = original

    ordered = list(normalized.values())
    # A single FAIL dominates.  Otherwise any unresolved REVIEW prevents PASS.
    if any(c["status"] == "non_compliant" for c in ordered):
        overall = "non_compliant"
    elif any(c["status"] == "needs_review" for c in ordered):
        overall = "needs_review"
    else:
        overall = "compliant"

    violations = _collect_issue_lines(ordered, "non_compliant")
    needs_review = _collect_issue_lines(ordered, "needs_review")

    result["checks"] = normalized
    result["compliance_status"] = overall
    result["compliance_display_status"] = _DISPLAY[overall]
    result["six_group_contract"] = {
        "version": "1.0",
        "ruleset_id": result.get("ruleset_id") or RULESET_ID,
        "groups": list(CHECK_ORDER),
        "overall_rule": "FAIL dominates REVIEW; REVIEW prevents PASS; N/A does not count as failure.",
    }

    for alias in ("lmpc", "pcr_2011"):
        finding = result.get(alias)
        if not isinstance(finding, dict):
            finding = {}
            result[alias] = finding
        finding["status"] = overall
        finding["is_compliant"] = overall == "compliant"
        finding["violations"] = violations
        finding["needs_review"] = needs_review

    return result
