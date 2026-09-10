"""Versioned Legal Metrology screening metadata used by LableLens.

The rules module deliberately keeps legal-reference metadata out of OCR regexes.
LableLens is a compliance screening aid; it does not replace the current
Gazette text, category-specific legislation, exemptions, or an authorised
physical inspection.
"""
from __future__ import annotations

from typing import Dict, Any

RULESET_ID = "LMPC-SIX-2026-09-10.3"
RULESET_REVIEWED = "2026-09-10"
RULESET_TITLE = (
    "Legal Metrology (Packaged Commodities) Rules, 2011 — "
    "six-core-check screening baseline"
)

# Official Department of Consumer Affairs material used to keep this screening
# baseline auditable.  The 2026 Rule 6(10A) country-of-origin filter amendment
# was subsequently shifted to 1 July 2027 by the Second Amendment Rules, 2026;
# therefore it is shown as a future platform-level requirement, not a current
# per-product pass/fail criterion in September 2026.
PRIMARY_SOURCES = [
    {
        "label": "Department of Consumer Affairs — Legal Metrology overview",
        "url": "https://consumeraffairs.gov.in/pages/legal-metrology-overview",
    },
    {
        "label": "Department of Consumer Affairs — LMPC Acts & Rules index",
        "url": "https://consumeraffairs.gov.in/pages/legal-metrology-act",
    },
    {
        "label": "LMPC Amendment Rules, 2021 — unit sale price insertion",
        "url": "https://consumeraffairs.gov.in/public/upload/admin/cmsfiles/whatsnews/The_Legal_Metrology_Packaged_Commodities_Amendment_Rule%2C_2021_whatsnews.pdf",
    },
    {
        "label": "LMPC Amendment Rules, 2022 — unit sale price wording / proviso",
        "url": "https://consumeraffairs.gov.in/public/upload/files/GSR226_1732871458.pdf",
    },
    {
        "label": "Department FAQ — Packaged Commodities Rules, 2011",
        "url": "https://consumeraffairs.gov.in/public/upload/admin/cmsfiles/whatsnews/FAQs_on_Packaged_Commodities%2C_Rules_2011_whatsnews.pdf",
    },
    {
        "label": "LMPC Second Amendment Rules, 2026 — Rule 6(10A) effective 1 July 2027",
        "url": "https://consumeraffairs.gov.in/public/upload/files/2026.4.27%20PCR%202nd%20COO%20from%201.7.2027_1777348487.pdf",
    },
]

CHECKS: Dict[str, Dict[str, Any]] = {
    "responsible_entity": {
        "number": 1,
        "title": "Commodity identity & responsible entity",
        "rules": "Rule 6(1) (name/address, generic name and imported-product declarations as applicable); Rule 10",
        "description": (
            "Common/generic commodity name plus manufacturer/packer/importer identity and a "
            "locatable postal address. Imported-product context also triggers country-of-origin screening."
        ),
    },
    "net_quantity": {
        "number": 2,
        "title": "Net quantity",
        "rules": "Rule 6(1)(c); Rules 11–13",
        "description": (
            "Declared positive quantity in a recognised unit of weight, measure or number. "
            "The photograph cannot verify the actual physical contents."
        ),
    },
    "mrp": {
        "number": 3,
        "title": "MRP & unit sale price",
        "rules": "Rule 6(1)(e); Rule 6(11); Rule 18",
        "description": (
            "Retail sale price in Indian currency, inclusive-of-all-taxes wording, and unit sale price "
            "where Rule 6(11) applies. The Rule 6(11) proviso is accounted for when MRP equals unit sale price."
        ),
    },
    "date": {
        "number": 4,
        "title": "Month/year & shelf-life declarations",
        "rules": "Rule 6(1)(d) and applicable provisos; Rule 6(10) for e-commerce display",
        "description": (
            "Manufacture/packing/import month-year for physical packages, plus expiry/use-by evidence when "
            "the commodity is one that may become unfit for human consumption. Rule 6(10) excludes the "
            "month/year display from the e-commerce network requirement."
        ),
    },
    "consumer_care": {
        "number": 5,
        "title": "Consumer care details",
        "rules": "Rule 6(2)",
        "description": (
            "Complaint-contact name/office, address, telephone and e-mail evidence. Candidate contact details "
            "remain reviewable when OCR cannot associate them with the consumer-care declaration."
        ),
    },
    "font_size": {
        "number": 6,
        "title": "Declaration font size",
        "rules": "Rule 7, Table I; Rule 7(3)",
        "description": (
            "Minimum numeral/letter height based on principal display panel (PDP) area. A calibrated physical "
            "millimetre measurement is required before this check can produce a defensible pass/fail."
        ),
    },
}

# Table-I minimum heights in millimetres.  Boundaries are explicit to avoid
# accidental off-by-one bucket logic.  The moulded/formed value for A <= 50 is
# 2.0 mm (not 1.5 mm).
FONT_TABLE = [
    (50.0, 1.0, 2.0),
    (100.0, 1.5, 3.0),
    (500.0, 2.5, 4.0),
    (2500.0, 4.0, 6.0),
    (float("inf"), 6.0, 6.0),
]

CATEGORY_PROFILES: Dict[str, Dict[str, Any]] = {
    "general": {
        "label": "General packaged commodity",
        "note": "Use the six-core LMPC screening baseline directly, subject to exemptions and commodity-specific provisions.",
    },
    "food": {
        "label": "Food regulated by FSSAI",
        "note": (
            "Department FAQ guidance identifies MRP, net quantity and consumer-care details as the Legal Metrology declarations "
            "for food products governed by FSSAI. Manufacturer/generic-name/date requirements may arise under food labelling law and are "
            "surfaced as cross-law review rather than an LMPC failure in this profile."
        ),
    },
    "cosmetics": {
        "label": "Cosmetic",
        "note": "Cosmetics have sector-specific labelling rules. LableLens applies common LMPC checks conservatively and flags legal-scope review where needed.",
    },
    "medical_device": {
        "label": "Medical device",
        "note": "Medical devices may have sector-specific declarations and exemptions. LableLens does not convert unresolved scope questions into automatic violations.",
    },
    "electronics": {
        "label": "Electronic product",
        "note": "Electronic products can have special declaration/QR provisions. Review current category-specific amendments before enforcement action.",
    },
    "other": {
        "label": "Other / uncertain category",
        "note": "Applicability is uncertain; the engine remains conservative and surfaces scope questions for review.",
    },
}

FUTURE_REQUIREMENTS = [
    {
        "effective": "2027-07-01",
        "rule": "Rule 6(10A)",
        "scope": "e-commerce platform",
        "description": (
            "E-commerce entities offering imported products must ensure product listings are available with "
            "a searchable and sortable country-of-origin filter. This is platform-level functionality and is "
            "not inferred from one listing screenshot/text block."
        ),
    }
]


def required_font_height_mm(pdp_area_cm2: float, moulded: bool = False) -> float:
    area = max(float(pdp_area_cm2), 0.0)
    for upper, normal, moulded_value in FONT_TABLE:
        if area <= upper:
            return moulded_value if moulded else normal
    return 6.0


def metadata() -> Dict[str, Any]:
    return {
        "id": RULESET_ID,
        "title": RULESET_TITLE,
        "reviewed": RULESET_REVIEWED,
        "checks": CHECKS,
        "primary_sources": PRIMARY_SOURCES,
        "future_requirements": FUTURE_REQUIREMENTS,
        "category_profiles": CATEGORY_PROFILES,
        "disclaimer": (
            "Screening aid only. The six groups organise common automated checks; they are not an exhaustive "
            "substitute for every exemption, category-specific declaration, dimension requirement, later "
            "notification, physical quantity test, placement assessment or enforcement conclusion."
        ),
    }
