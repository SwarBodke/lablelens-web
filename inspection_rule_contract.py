"""Original-2011 six-group Legal Metrology result policy for LableLens.

Smart OCR and field extraction happen before this module.  This policy layer
rebuilds the active compliance checks using only the principal Legal Metrology
(Packaged Commodities) Rules, 2011 baseline selected for this project.

It deliberately removes later-amendment-only scoring such as unit sale price,
country-of-origin insertion, e-commerce display rules and the later shelf-life
clause. Raw OCR evidence remains available internally for audit/debugging.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, Optional

from ruleset import (
    CHECKS,
    RULESET_ID,
    CATEGORY_PROFILES,
    required_original_letter_height_mm,
    required_original_numeral_height_mm,
)


CHECK_ORDER = (
    "responsible_entity",
    "net_quantity",
    "date",
    "mrp",
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
_COMPLETE_SCOPES = {"complete", "complete_package", "all_panels", "complete_evidence"}


def _status(value: Any) -> str:
    value = str(value or "needs_review").strip().lower()
    return value if value in _ALLOWED else "needs_review"


def _missing_status(result: Dict[str, Any]) -> str:
    """Absence from a web listing is never a 2011 package-law FAIL.

    The original 2011 baseline did not contain the later e-commerce display
    provision. A complete physical package can support an absence finding;
    partial physical evidence or listing text remains REVIEW.
    """
    if str(result.get("source_type") or "physical").lower() == "ecommerce":
        return "needs_review"
    scope = str(result.get("evidence_scope") or "partial").lower()
    return "non_compliant" if scope in _COMPLETE_SCOPES else "needs_review"


def _make_check(
    key: str,
    status: str,
    summary: str,
    *,
    evidence: Any = None,
    issues: Optional[list[str]] = None,
    subchecks: Optional[list[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    meta = CHECKS[key]
    st = _status(status)
    subs = []
    for sub in subchecks or []:
        s = deepcopy(sub)
        sub_st = _status(s.get("status"))
        s["status"] = sub_st
        s["display_status"] = _DISPLAY[sub_st]
        subs.append(s)
    return {
        "id": key,
        "number": meta["number"],
        "title": meta["title"],
        "rule": meta["rules"],
        "status": st,
        "display_status": _DISPLAY[st],
        "summary": summary,
        "evidence": evidence,
        "issues": issues or [],
        "subchecks": subs,
    }


def _combine_subchecks(subchecks: list[Dict[str, Any]]) -> str:
    active = [_status(x.get("status")) for x in subchecks if _status(x.get("status")) != "not_applicable"]
    if "non_compliant" in active:
        return "non_compliant"
    if "needs_review" in active:
        return "needs_review"
    if active:
        return "compliant"
    return "not_applicable"


def _responsible_entity_check(result: Dict[str, Any], missing: str) -> Dict[str, Any]:
    manufacturer = result.get("manufacturer") if isinstance(result.get("manufacturer"), dict) else {}
    company = result.get("company") or manufacturer.get("name")
    address = manufacturer.get("address")
    address_complete = bool(manufacturer.get("address_complete"))
    generic = result.get("generic_name")
    product_name = str(result.get("product_name") or "").strip()
    commodity_identity = generic or (product_name if product_name and product_name.lower() != "unnamed product" else None)

    subs: list[Dict[str, Any]] = []
    issues: list[str] = []

    st = "compliant" if company else missing
    subs.append({"name": "Manufacturer / packer / importer name", "status": st, "value": company})
    if not company:
        issues.append("Manufacturer/packer/importer name was not established from the supplied package evidence.")

    st = "compliant" if address_complete else missing
    subs.append({"name": "Responsible entity address", "status": st, "value": address})
    if not address_complete:
        issues.append("A complete responsible-entity address was not established from the supplied package evidence.")

    # Rule 6(1)(b) requires the common/generic commodity name, but the Rule does
    # not require a literal field caption reading 'Generic Name'.  Therefore a
    # credible OCR product/commodity identity can satisfy this grouped sub-check.
    st = "compliant" if commodity_identity else missing
    subs.append({
        "name": "Commodity identity (Rule 6(1)(b))",
        "status": st,
        "value": commodity_identity,
        "note": "Internal sub-check only; LableLens does not expose a separate top-level 'Generic Name' rule and does not require that literal caption.",
    })
    if not commodity_identity:
        issues.append("A common/generic commodity identity was not established from the supplied evidence.")

    status = _combine_subchecks(subs)
    summary = f"Responsible entity: {company or 'not detected'}"
    if commodity_identity:
        summary += f"; commodity identity: {commodity_identity}"
    return _make_check(
        "responsible_entity",
        status,
        summary,
        evidence={"company": company, "role": manufacturer.get("role"), "address": address, "commodity_identity": commodity_identity},
        issues=issues,
        subchecks=subs,
    )


def _net_quantity_check(result: Dict[str, Any], missing: str) -> Dict[str, Any]:
    qty = result.get("quantity_details") if isinstance(result.get("quantity_details"), dict) else {}
    display = qty.get("display") or result.get("quantity")
    issues: list[str] = []
    subs: list[Dict[str, Any]] = []

    if not display:
        subs.append({"name": "Net quantity declaration", "status": missing, "value": None})
        issues.append("Net quantity was not detected in the supplied evidence.")
    else:
        value = qty.get("value")
        if value is not None:
            try:
                positive = float(value) > 0
            except Exception:
                positive = False
        else:
            positive = True
        value_status = "compliant" if positive else "non_compliant"
        subs.append({"name": "Net quantity declaration", "status": value_status, "value": display})
        if not positive:
            issues.append("Detected net quantity is not a positive value.")

        unit_violation = qty.get("unit_violation") or result.get("unit_violation")
        unit_status = "non_compliant" if unit_violation else "compliant"
        subs.append({"name": "Standard unit expression", "status": unit_status, "value": qty.get("unit_raw") or qty.get("unit")})
        if unit_violation:
            issues.append(f"Non-standard unit expression '{qty.get('unit_raw') or unit_violation}' was detected.")

    return _make_check(
        "net_quantity",
        _combine_subchecks(subs),
        str(display or "Net quantity not detected."),
        evidence=qty,
        issues=issues,
        subchecks=subs,
    )


def _date_check(result: Dict[str, Any], missing: str) -> Dict[str, Any]:
    mfg = result.get("mfg_date") if isinstance(result.get("mfg_date"), dict) else result.get("mfg_date")
    category = str(result.get("category") or "general").lower()
    source_type = str(result.get("source_type") or "physical").lower()

    # The original Rule 6(1)(d) itself defers this declaration for food and
    # cosmetics to other legislation. This 2011-only baseline does not evaluate
    # those external laws, so it does not manufacture a cross-law PASS/FAIL.
    if category in {"food", "cosmetics"}:
        return _make_check(
            "date",
            "not_applicable",
            "Original Rule 6(1)(d) contains a sector-law proviso for this category; external law is outside this 2011-only ruleset.",
            evidence={"mfg_date": mfg},
            subchecks=[{
                "name": "Manufacture / packing / import month-year",
                "status": "not_applicable",
                "value": mfg,
                "note": "No external sector-law requirement is scored by LableLens in this baseline.",
            }],
        )

    if mfg:
        value = mfg.get("display") if isinstance(mfg, dict) else str(mfg)
        if not value and isinstance(mfg, dict) and mfg.get("month") and mfg.get("year"):
            value = f"{mfg.get('month')}/{mfg.get('year')}"
        return _make_check(
            "date",
            "compliant",
            f"Month/year evidence detected: {value or 'detected'}",
            evidence={"mfg_date": mfg},
            subchecks=[{"name": "Manufacture / packing / import month-year", "status": "compliant", "value": value or mfg}],
        )

    note = None
    if source_type == "ecommerce":
        note = "The original 2011 Rules did not contain the later e-commerce display provision; absence from listing text is not treated as package non-compliance."
    return _make_check(
        "date",
        missing,
        "Manufacture / packing / import month-year not detected.",
        evidence={"mfg_date": None},
        issues=["Required month/year evidence was not established from the supplied package evidence." if source_type != "ecommerce" else note],
        subchecks=[{"name": "Manufacture / packing / import month-year", "status": missing, "value": None, "note": note}],
    )


def _mrp_check(result: Dict[str, Any], missing: str) -> Dict[str, Any]:
    mrp = result.get("mrp")
    inclusive = bool(result.get("inclusive_tax"))
    issues: list[str] = []
    subs: list[Dict[str, Any]] = []

    if mrp is None:
        mrp_status = missing
        issues.append("Retail sale price / MRP was not detected.")
        mrp_value = None
    else:
        try:
            valid = float(mrp) >= 0
            mrp_value = f"₹{float(mrp):.2f}"
        except Exception:
            valid = False
            mrp_value = str(mrp)
        mrp_status = "compliant" if valid else "non_compliant"
        if not valid:
            issues.append("Detected MRP value could not be validated as a non-negative price.")
    subs.append({"name": "Retail sale price / MRP", "status": mrp_status, "value": mrp_value})

    if mrp is None:
        tax_status = missing
    else:
        tax_status = "compliant" if inclusive else missing
    subs.append({
        "name": "Inclusive-of-all-taxes wording",
        "status": tax_status,
        "value": inclusive,
        "note": "Part of the original 2011 retail-sale-price declaration form; this is not a unit-sale-price check.",
    })
    if mrp is not None and not inclusive:
        issues.append("MRP was detected but inclusive-of-all-taxes wording was not established from the supplied evidence.")

    summary = (mrp_value or "MRP not detected") + ("; inclusive-of-all-taxes wording detected" if inclusive else "; inclusive-tax wording not detected")
    return _make_check(
        "mrp",
        _combine_subchecks(subs),
        summary,
        evidence={"mrp": mrp, "inclusive_tax": inclusive},
        issues=issues,
        subchecks=subs,
    )


def _consumer_care_check(result: Dict[str, Any], missing: str) -> Dict[str, Any]:
    care = result.get("consumer_care") if isinstance(result.get("consumer_care"), dict) else {}
    subs: list[Dict[str, Any]] = []
    issues: list[str] = []

    required = [
        ("Consumer complaint person / office", care.get("name_or_office")),
        ("Consumer complaint address", care.get("address")),
        ("Telephone", care.get("phone")),
    ]
    for name, value in required:
        st = "compliant" if value else missing
        subs.append({"name": name, "status": st, "value": value})
        if not value:
            issues.append(f"{name} was not established from the supplied evidence.")

    email = care.get("email")
    subs.append({
        "name": "E-mail (if available)",
        "status": "compliant" if email else "not_applicable",
        "value": email,
        "note": "Original Rule 6(2) says 'E-mail address, if available'; absence alone is not scored as a failure.",
    })

    return _make_check(
        "consumer_care",
        _combine_subchecks(subs),
        care.get("block") or "Consumer complaint contact block not established.",
        evidence=care,
        issues=issues,
        subchecks=subs,
    )


def _font_check(result: Dict[str, Any]) -> Dict[str, Any]:
    if str(result.get("source_type") or "physical").lower() == "ecommerce":
        return _make_check(
            "font_size",
            "not_applicable",
            "Physical package character size and placement cannot be established from listing text under the original 2011 baseline.",
            subchecks=[{"name": "Physical declaration measurement", "status": "not_applicable", "value": None}],
        )

    raw_checks = result.get("checks") if isinstance(result.get("checks"), dict) else {}
    legacy_font = raw_checks.get("font_size") if isinstance(raw_checks.get("font_size"), dict) else {}
    ev = legacy_font.get("evidence") if isinstance(legacy_font.get("evidence"), dict) else {}

    height = ev.get("font_height_mm")
    width = ev.get("font_width_mm")
    area = ev.get("pdp_area_cm2")
    moulded = bool(ev.get("moulded_text"))
    qty = result.get("quantity_details") if isinstance(result.get("quantity_details"), dict) else {}

    try:
        height = float(height) if height is not None else None
    except Exception:
        height = None
    try:
        width = float(width) if width is not None else None
    except Exception:
        width = None
    try:
        area = float(area) if area is not None else None
    except Exception:
        area = None

    if height is None:
        return _make_check(
            "font_size",
            "needs_review",
            "Calibrated physical character height was not supplied; photo pixels alone are not converted into statutory millimetres.",
            evidence={"font_height_mm": None, "font_width_mm": width, "pdp_area_cm2": area, "moulded_text": moulded},
            issues=["A calibrated physical measurement is required for a defensible Rule 7 size finding."],
            subchecks=[
                {"name": "Original Rule 7 character height", "status": "needs_review", "value": None},
                {"name": "Rule 8 placement / clear space", "status": "not_applicable", "value": None, "note": "Not automatically inferred from OCR text alone."},
                {"name": "Rule 9 manner / legibility", "status": "not_applicable", "value": None, "note": "Requires visual/manual review when not objectively measurable."},
            ],
        )

    numeral_required = required_original_numeral_height_mm(qty.get("value"), qty.get("unit"), area, moulded=moulded)
    letter_required = required_original_letter_height_mm(moulded=moulded)
    issues: list[str] = []
    subs: list[Dict[str, Any]] = []

    if numeral_required is None:
        numeral_status = "needs_review"
        subs.append({
            "name": "Original Rule 7 numeral height",
            "status": numeral_status,
            "value": height,
            "note": "Numeral threshold could not be resolved because quantity/unit or required PDP-area information is incomplete.",
        })
        issues.append("Original Rule 7 numeral-height bucket could not be resolved automatically.")
    else:
        numeral_status = "compliant" if height + 1e-9 >= numeral_required else "non_compliant"
        subs.append({
            "name": "Original Rule 7 numeral height",
            "status": numeral_status,
            "value": height,
            "required_mm": numeral_required,
        })
        if numeral_status == "non_compliant":
            issues.append(f"Measured character height {height:g} mm is below the original Rule 7 numeral threshold of {numeral_required:g} mm.")

    letter_status = "compliant" if height + 1e-9 >= letter_required else "non_compliant"
    subs.append({"name": "Original Rule 7 letter height", "status": letter_status, "value": height, "required_mm": letter_required})
    if letter_status == "non_compliant":
        issues.append(f"Measured character height {height:g} mm is below the original Rule 7 letter threshold of {letter_required:g} mm.")

    if width is None:
        width_status = "needs_review"
        subs.append({"name": "Character width (minimum one-third of height)", "status": width_status, "value": None})
        issues.append("Character width was not supplied for the Rule 7 width check.")
    else:
        width_status = "compliant" if width + 1e-9 >= height / 3.0 else "non_compliant"
        subs.append({"name": "Character width (minimum one-third of height)", "status": width_status, "value": width, "minimum_mm": height / 3.0})
        if width_status == "non_compliant":
            issues.append(f"Measured character width {width:g} mm is less than one-third of measured height {height:g} mm, subject to the original character exceptions.")

    subs.extend([
        {"name": "Rule 8 placement / quantity clear space", "status": "not_applicable", "value": None, "note": "Not automatically inferred without calibrated layout measurements."},
        {"name": "Rule 9 manner / legibility", "status": "not_applicable", "value": None, "note": "Not converted into a PASS/FAIL solely from OCR confidence."},
    ])

    threshold_note = f"numeral threshold {numeral_required:g} mm" if numeral_required is not None else "numeral threshold unresolved"
    return _make_check(
        "font_size",
        _combine_subchecks(subs),
        f"Measured character height {height:g} mm; {threshold_note}; letter threshold {letter_required:g} mm.",
        evidence={
            "font_height_mm": height,
            "font_width_mm": width,
            "pdp_area_cm2": area,
            "moulded_text": moulded,
            "required_numeral_height_mm": numeral_required,
            "required_letter_height_mm": letter_required,
        },
        issues=issues,
        subchecks=subs,
    )


def _collect_issue_lines(checks: Iterable[Dict[str, Any]], target_status: str) -> list[str]:
    out: list[str] = []
    for check in checks:
        if check.get("status") != target_status:
            continue
        title = check.get("title") or "Compliance check"
        issues = check.get("issues") or []
        if not issues and check.get("summary"):
            issues = [check.get("summary")]
        for issue in issues:
            if not issue:
                continue
            line = f"{title}: {issue}"
            if line not in out:
                out.append(line)
    return out


def finalize_six_group_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild an engine result as the selected original-2011 LMPC baseline."""
    if not isinstance(result, dict):
        return result

    missing = _missing_status(result)
    rebuilt: Dict[str, Dict[str, Any]] = {}
    rebuilt["responsible_entity"] = _responsible_entity_check(result, missing)
    rebuilt["net_quantity"] = _net_quantity_check(result, missing)
    rebuilt["date"] = _date_check(result, missing)
    rebuilt["mrp"] = _mrp_check(result, missing)
    rebuilt["consumer_care"] = _consumer_care_check(result, missing)
    rebuilt["font_size"] = _font_check(result)

    # Keep API order deterministic and aligned with the six presentation groups.
    normalized = {key: rebuilt[key] for key in CHECK_ORDER}
    ordered = list(normalized.values())

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
    result["ruleset_id"] = RULESET_ID
    result["category_profile"] = CATEGORY_PROFILES.get(str(result.get("category") or "general").lower(), CATEGORY_PROFILES["other"])
    result["regulatory_scope"] = "Original Legal Metrology (Packaged Commodities) Rules, 2011 — G.S.R. 202(E) baseline; later amendments excluded"

    # Later-amendment-only fields must not appear as active compliance outputs.
    # Raw OCR text remains stored internally and can still contain those words.
    result.pop("unit_sale_price", None)
    result.pop("country_of_origin", None)

    # Recalculate a simple extraction-completeness indicator using only evidence
    # relevant to this baseline. N/A legal requirements do not count as missing.
    active_subchecks = []
    for check in ordered:
        for sub in check.get("subchecks") or []:
            if sub.get("status") != "not_applicable":
                active_subchecks.append(sub)
    if active_subchecks:
        satisfied = sum(1 for sub in active_subchecks if sub.get("status") == "compliant")
        result["extraction_completeness"] = round(100 * satisfied / len(active_subchecks))

    result["six_group_contract"] = {
        "version": "2.0-original-2011",
        "ruleset_id": RULESET_ID,
        "groups": list(CHECK_ORDER),
        "overall_rule": "FAIL dominates REVIEW; REVIEW prevents PASS; N/A does not count as failure.",
        "later_amendments_excluded": True,
        "unit_sale_price_checked": False,
        "generic_name_top_level_check": False,
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
        finding["ruleset_id"] = RULESET_ID

    return result
