"""Original-2011 Legal Metrology (Packaged Commodities) Rules baseline.

This module is intentionally historical and source-scoped.  It represents the
principal Rules as notified by G.S.R. 202(E) on 7 March 2011 and effective from
1 April 2011.  It is NOT a representation of the fully amended Rules currently
in force.

LableLens uses this baseline because the project has explicitly chosen the
original 2011 rule set for its screening database.  Later amendment-only
requirements (for example unit sale price, the later imported-product country
of origin clause, e-commerce display provisions and the later shelf-life
clause) are not active checks in this ruleset.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


RULESET_ID = "LMPC-2011-GSR202E-BASELINE-v1"
RULESET_REVIEWED = "2026-09-11"
RULESET_TITLE = "Legal Metrology (Packaged Commodities) Rules, 2011 — original G.S.R. 202(E) baseline"
RULESET_EFFECTIVE_FROM = "2011-04-01"
RULESET_HISTORICAL = True

PRIMARY_SOURCES = [
    {
        "label": "Department of Consumer Affairs — original Legal Metrology (Packaged Commodities) Rules, 2011, G.S.R. 202(E), 7 March 2011",
        "url": "https://consumeraffairs.gov.in/public/upload/files/8_1732871406.pdf",
    },
    {
        "label": "Department of Consumer Affairs — Legal Metrology Acts & Rules index",
        "url": "https://consumeraffairs.gov.in/pages/legal-metrology-act",
    },
]

# Six presentation groups used by LableLens.  They organise the original 2011
# provisions without turning every clause into a separate top-level card.
CHECKS: Dict[str, Dict[str, Any]] = {
    "responsible_entity": {
        "number": 1,
        "title": "Commodity Identity & Responsible Entity",
        "rules": "Rule 6(1)(a), Rule 6(1)(b); Rule 10",
        "description": (
            "Manufacturer/packer/importer identity and address as applicable. "
            "The common/generic commodity name required by Rule 6(1)(b) is kept "
            "inside this group as a sub-check; it is not a separate top-level parameter."
        ),
    },
    "net_quantity": {
        "number": 2,
        "title": "Net Quantity",
        "rules": "Rule 6(1)(c); Rules 11–13",
        "description": (
            "Presence of the declared net quantity in the applicable standard unit of "
            "weight, measure or number. Image screening does not verify the actual physical contents."
        ),
    },
    "date": {
        "number": 3,
        "title": "Manufacture / Packing / Import Date",
        "rules": "Rule 6(1)(d) and its original provisos",
        "description": (
            "Month and year of manufacture, pre-packing or import as applicable under the "
            "original 2011 text. Later best-before/use-by amendments are not part of this baseline."
        ),
    },
    "mrp": {
        "number": 4,
        "title": "Maximum Retail Price (MRP)",
        "rules": "Rule 2(m); Rule 6(1)(e); Rule 18",
        "description": (
            "Retail sale price / MRP evidence in the form required by the original 2011 "
            "Rules, including the inclusive-of-all-taxes wording. Unit sale price is not checked."
        ),
    },
    "consumer_care": {
        "number": 5,
        "title": "Consumer Care Details",
        "rules": "Rule 6(2)",
        "description": (
            "Name/office, address and telephone contact for consumer complaints. "
            "The original 2011 text qualifies e-mail with 'if available', so absence of an e-mail "
            "address alone is not treated as non-compliance by this baseline."
        ),
    },
    "font_size": {
        "number": 6,
        "title": "Declaration Presentation & Font",
        "rules": "Rules 7–9; original Table I and Table II",
        "description": (
            "Original 2011 numeral/letter size rules and presentation requirements. "
            "A defensible automated size result requires calibrated physical measurements; "
            "photo pixels alone are not converted into statutory millimetres."
        ),
    },
}

# Original Rule 7 Table I: minimum NUMERAL height where net quantity is by
# weight or volume.  Values are (upper quantity in g/ml, normal mm, formed mm).
ORIGINAL_TABLE_I = [
    (200.0, 1.0, 2.0),
    (500.0, 2.0, 4.0),
    (float("inf"), 4.0, 6.0),
]

# Original Rule 7 Table II: minimum NUMERAL height where net quantity is by
# length, area or number.  The bucket is based on principal display panel area.
# Values are (upper PDP area cm2, normal mm, formed mm).
ORIGINAL_TABLE_II = [
    (100.0, 1.0, 2.0),
    (500.0, 2.0, 4.0),
    (2500.0, 4.0, 6.0),
    (float("inf"), 6.0, 6.0),
]

# Backward-compatible alias used by legacy code before the 2011 policy
# post-processor rebuilds the active font result.
FONT_TABLE = ORIGINAL_TABLE_II


def required_font_height_mm(pdp_area_cm2: float, moulded: bool = False) -> float:
    """Compatibility helper using the original Table-II PDP-area buckets."""
    area = max(float(pdp_area_cm2), 0.0)
    for upper, normal, formed in ORIGINAL_TABLE_II:
        if area <= upper:
            return formed if moulded else normal
    return 6.0


def required_original_numeral_height_mm(
    quantity_value: Optional[float],
    quantity_unit: Optional[str],
    pdp_area_cm2: Optional[float],
    moulded: bool = False,
) -> Optional[float]:
    """Return the original Rule-7 numeral-height threshold when resolvable.

    Weight/volume declarations use original Table I after conversion to g/ml.
    Length/number declarations use original Table II and therefore require PDP
    area.  Unsupported/unknown units return None rather than guessing.
    """
    unit = str(quantity_unit or "").strip()
    try:
        value = float(quantity_value) if quantity_value is not None else None
    except Exception:
        value = None

    q_g_ml: Optional[float] = None
    if value is not None and value >= 0:
        if unit == "mg":
            q_g_ml = value / 1000.0
        elif unit == "g":
            q_g_ml = value
        elif unit == "kg":
            q_g_ml = value * 1000.0
        elif unit == "ml":
            q_g_ml = value
        elif unit == "L":
            q_g_ml = value * 1000.0

    if q_g_ml is not None:
        for upper, normal, formed in ORIGINAL_TABLE_I:
            if q_g_ml <= upper:
                return formed if moulded else normal

    if unit in {"cm", "m", "pcs"}:
        try:
            area = float(pdp_area_cm2) if pdp_area_cm2 is not None else None
        except Exception:
            area = None
        if area is None or area < 0:
            return None
        for upper, normal, formed in ORIGINAL_TABLE_II:
            if area <= upper:
                return formed if moulded else normal

    return None


def required_original_letter_height_mm(moulded: bool = False) -> float:
    """Original Rule 7(3): letters >=1 mm, or >=2 mm when formed/etc."""
    return 2.0 if moulded else 1.0


# Category values remain for API/UI compatibility only.  They do not import or
# score FSSAI, cosmetics, medical-device, electronics or other sector-law rules.
CATEGORY_PROFILES: Dict[str, Dict[str, Any]] = {
    "general": {
        "label": "General packaged commodity",
        "note": "Original-2011 LMPC baseline only; subject to the provisos and exemptions in that text.",
    },
    "food": {
        "label": "Food / beverage",
        "note": "No external food-law requirements are evaluated. Where the original 2011 text defers a declaration to another law, LableLens does not invent a cross-law failure.",
    },
    "cosmetics": {
        "label": "Cosmetic",
        "note": "No cosmetics-law requirements are evaluated. Original-2011 LMPC provisos are handled conservatively.",
    },
    "medical_device": {
        "label": "Medical device",
        "note": "No medical-device law is evaluated; only the original-2011 LMPC baseline is scored.",
    },
    "electronics": {
        "label": "Electronic product",
        "note": "No electronics-specific later amendment or sector-law requirement is evaluated.",
    },
    "other": {
        "label": "Other / uncertain category",
        "note": "Only the original-2011 LMPC baseline is scored; unresolved applicability remains reviewable.",
    },
}

# Later amendment requirements are deliberately absent from this historical
# baseline.  Kept as an empty field for API compatibility.
FUTURE_REQUIREMENTS = []

EXCLUDED_LATER_AMENDMENTS = [
    "Unit sale price / Rule 6(11)",
    "Imported-product country-of-origin clause inserted later as Rule 6(1)(aa)",
    "E-commerce display provisions introduced after the principal 2011 Rules",
    "Later best-before/use-by clause Rule 6(1)(da)",
]

CONDITIONAL_OR_MANUAL_PROVISIONS = [
    "Rule 6(1)(f): dimensions where the size of the commodity is relevant",
    "Original Rule 6 provisos/exemptions for particular commodities",
    "Rule 8 placement and clear-space requirements where image geometry is not calibrated",
    "Rule 9 manner/legibility matters that cannot be established reliably from OCR text alone",
]


def metadata() -> Dict[str, Any]:
    return {
        "id": RULESET_ID,
        "title": RULESET_TITLE,
        "reviewed": RULESET_REVIEWED,
        "effective_from": RULESET_EFFECTIVE_FROM,
        "historical_baseline": RULESET_HISTORICAL,
        "checks": CHECKS,
        "primary_sources": PRIMARY_SOURCES,
        "future_requirements": FUTURE_REQUIREMENTS,
        "category_profiles": CATEGORY_PROFILES,
        "excluded_later_amendments": EXCLUDED_LATER_AMENDMENTS,
        "conditional_or_manual_provisions": CONDITIONAL_OR_MANUAL_PROVISIONS,
        "disclaimer": (
            "Historical screening baseline based on the principal Legal Metrology (Packaged Commodities) "
            "Rules, 2011 as notified by G.S.R. 202(E). It intentionally excludes later amendments and "
            "does not represent the fully amended law currently in force. LableLens is a screening aid, "
            "not an enforcement order; statutory exemptions and unresolved physical matters require review."
        ),
    }
