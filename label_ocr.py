r"""
Tolmaap — Product Label OCR Engine
Legacy extraction helpers for packaged-commodity declarations from product label images.

Fixes applied vs original:
  - company: group(1) instead of group(0)
  - normalize_unit: keys normalised to lowercase before lookup
  - MRP_FALSE_POSITIVES: now actually applied in extract_mrp()
  - Removed two overly-broad MRP fallback patterns
  - QTY decimal support: (\d+(?:\.\d+)?) instead of (\d+(?:\d+)?)
  - Removed duplicate 'ml' unit in QTY pattern
  - Fixed empty-alternative || in COMPANY regex
  - Added cv2.imdecode() path for in-memory bytes (FastAPI uploads)
  - Added packaged-commodity fields: "inclusive of all taxes" check, consumer-care, unit-normalisation
  - PSM 11 (sparse text) for full-image pass; PSM 6 for zones
"""

import cv2
import pytesseract
import re
import numpy as np
import json
import argparse
import os
import shutil
from pathlib import Path
from datetime import datetime


# ============================================================
# CONFIGURATION
# ============================================================

class Config:
    # Portable Tesseract discovery.  An explicit TESSERACT_CMD wins; otherwise
    # use PATH.  Windows installation folders are only fallbacks.
    _tess_candidates = [os.getenv("TESSERACT_CMD", "").strip(), shutil.which("tesseract") or ""]
    if os.name == "nt":
        _tess_candidates += [
            r'C:\Program Files\Tesseract-OCR\tesseract.exe',
            r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
        ]
    for _candidate in _tess_candidates:
        if _candidate and (os.path.isfile(_candidate) or _candidate == shutil.which("tesseract")):
            pytesseract.pytesseract.tesseract_cmd = _candidate
            break

    # Image quality
    MIN_IMAGE_WIDTH  = 300
    UPSCALE_FACTOR   = 3
    BOTTOM_ZONE_START = 0.70
    TOP_ZONE_END      = 0.40

    # Confidence thresholds
    HIGH_CONFIDENCE   = 80
    MEDIUM_CONFIDENCE = 50

    # MRP valid range (₹)
    MIN_MRP = 1
    MAX_MRP = 50_000


# ============================================================
# PATTERN DEFINITIONS
# ============================================================

class Patterns:

    # ── MRP ──────────────────────────────────────────────────────────────────
    MRP = [
        # With explicit MRP / Max Retail keyword — noise budget after the
        # keyword tolerates OCR-misread currency symbols/stray characters
        # (verified: real photos showed ₹ misread as both "%" and "®", plus
        # stray characters like "+" appearing between "MRP" and the number)
        r"(?:MRP|M\.R\.P|Max\.?\s*Retail\s*Price|MAX\s*RETAIL)[:\s]*[^\d\n]{0,6}(\d{1,5}(?:[.,]\d{1,2})?)",
        # Currency symbol immediately before the number
        r"(?:₹|Rs\.?)\s*(\d{1,5}(?:[.,]\d{1,2})?)",
        # Number followed by /- (common MRP notation on Indian labels)
        r"(\d{1,5}(?:[.,]\d{1,2})?)\s*/\s*-",
    ]

    # Patterns that look like MRP but are NOT — applied as a blocklist
    # FIX: removed the blanket "\b\d{4}\b" (4-digit) rejection rule — it was
    # silently rejecting every legitimate 4-digit price (₹1000–9999, the
    # normal range for electronics/appliances) on the theory it might be a
    # year. But every MRP pattern above already requires "MRP"/₹/Rs context
    # to match in the first place, so a 4-digit number captured there is
    # essentially always a real price, not a year — the blocklist was doing
    # more harm than the false positive it was guarding against.
    MRP_FALSE_POSITIVES = [
        r"^\d{1,2}\.\d{2}$",       # tiny decimal like "1.25"
        r"\d+\s*%",                 # percentages
        r"\d+\s*[\+\-]\s*\d+",     # math expressions
    ]

    # ── Quantity ──────────────────────────────────────────────────────────────
    # FIX: (\d+(?:\.\d+)?) supports decimals like 500.5 ml, 1.5 L
    # FIX: removed duplicate 'ml' entries
    UNITS = r"(ml|l|g|kg|mg|gm|litre|liter|piece|pcs|tab|caps|no|nos)"
    # Bare "n" (as in "1N") only allowed in the LABELED pattern below (right
    # next to "Net Qty"), not the generic bare fallback — a single stray
    # letter "n" anywhere in noisy OCR text is too easy to false-match.
    UNITS_LABELED = r"(ml|l|g|kg|mg|gm|litre|liter|piece|pcs|tab|caps|no|nos|n|unit|units)"

    QTY = [
        r"(?:Net\s*(?:Wt|Weight|Content|Vol|Volume|Qty|Quantity)|Contents?)[:\s]*(\d+(?:\.\d+)?)\s*" + UNITS_LABELED,
        r"\b(\d+(?:\.\d+)?)\s*" + UNITS + r"\b",
    ]

    # Non-standard legacy unit strings flagged for review
    NON_STANDARD_UNITS = {
        "gms": "g", "grm": "g", "grms": "g",
        "kgs": "kg",
        "ltr": "L", "ltrs": "L", "lts": "L",
        "mls": "ml",
    }

    # ── Manufacturing date ───────────────────────────────────────────────────
    MFG = [
        # Direct keyword on same line or within short prefix
        r"(?:Mfg\.?\s*Date|Mfd\.?\s*Date|Mfg|Mfd|MFD|Manufacturing\s*Date|Manufactured\s*On|"
        r"Manufactured|Manufacturing|Date\s*of\s*(?:Mfg|Manufacturing|Packing)|"
        r"Month\s*(?:&|and)?\s*Year\s*of\s*Manufactur(?:ing|e|or|oring)|"
        r"PKD|Pkd|Packed|Packing\s*Date|Daeg|Date)[:\s]*"
        r"(\d{1,2}[\s\/\-\.—]\w{3,}[\s\/\-\.—]\d{2,4}"
        r"|\w{3,}[\s\/\-\.—]\d{2,4}"
        r"|\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}"
        r"|\d{1,2}[\/\-\.7|Il\s]\d{4})",
        # Multi-line / curved surface cluster: keyword/batch prefix followed by dot-matrix date
        r"(?:Mfg|Mfd|MFD|Date|Daeg|Batch|Lot|BDRB|BORB|PKD|Pkd)[\w\s,.:\-]{0,50}?\b((?:0[1-9]|1[0-2])[\/\-\.7|Il\s]20\d{2})\b",
        # Standalone dot-matrix MM/YYYY fallback (valid month 01-12, year 2020-2035)
        r"\b((?:0[1-9]|1[0-2])[\/\-\.7|Il\s]20[2-3]\d)\b",
    ]

    # ── Expiry date ──────────────────────────────────────────────────────────
    EXP = [
        r"(?:Exp\.?\s*Date|Expiry\s*Date|Expiration\s*Date|Best\s*Before|Use\s*Before|Use\s*By|Best\s*By)[:\s]*"
        r"(\d{1,2}[\s\/\-\.—]\w{3,}[\s\/\-\.—]\d{2,4}"
        r"|\w{3,}[\s\/\-\.—]\d{2,4}"
        r"|\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}"
        r"|\d{1,2}[\/\-\.7|Il\s]\d{4})",
        # Multi-line / curved surface cluster
        r"(?:Exp|Expiry|Use\s*Before|Best\s*Before)[\w\s,.:\-]{0,50}?\b((?:0[1-9]|1[0-2])[\/\-\.7|Il\s]20\d{2})\b",
    ]

    # ── Shelf life statements (PCR 2011 compliant expiry declaration) ─────────
    SHELF_LIFE = [
        r"(?:(?:use|best)\s*before|(?:use|best)\s*within|shelf\s*life|exp(?:iry)?)?[:\s]*"
        r"(\d{1,2})\s*(?:months?|moths?|menths?)\s*"
        r"(?:from|of|after)\s*"
        r"(?:the\s*)?(?:date\s*of\s*)?"
        r"(?:mfg|mig|mfd|manufactur(?:ing|e|or|oring)|pack(?:ing|aging)?|pkd)?",
    ]

    # ── Company ──────────────────────────────────────────────────────────────
    COMPANY = [
        # Compound prefix: "Marketed & Customer Care by : IDAM Natural Wellness Pvt. Ltd."
        r"(?:(?:Marketed|Mfg|Manufactured|Made|Packed|Distributed|Imported|Merkated)\s*(?:&|and)?\s*(?:Customer\s*Care\s*)?(?:by|for|at|bys|by;)?[:\s]*)[ \t]*"
        r"([\w\s&'.\-]+?(?:Pvt\.?\s*Ltd|Ltd|Limited|Co\.|Corp\.|Inc\.|LLP|PLC)[.,]?)",
        # Standard Mfg by / Marketed by
        r"(?:Mfg|Manufactured|Made|Packed|Marketed|Imported|Distributed)\s+(?:by|for|at)[:\s]*[^\w\d\s]{0,2}\s*"
        r"([\w\s&'.\-]+?(?:Pvt\.?\s*Ltd|Ltd|Limited|Co\.|Corp\.|Inc\.|LLP|PLC)[.,]?)",
        # Standard Mfg by / Marketed by followed by general company/brand name
        r"(?:Mfg|Manufactured|Made|Packed|Marketed|Imported|Distributed)\s+(?:by|for|at)[:\s]*[^\w\d\s]{0,2}\s*"
        r"([A-Za-z0-9\s&'.\-]{3,60}?)(?:[.,;]|\b(?:Plot|Phase|Sector|Road|Street|Near|Opp|Tel|Ph|Email|Customer|Consumer|Net|MRP|Batch|MFD|EXP)\b|$)",
        # Standalone legal entity
        r"\b([A-Za-z0-9\s&'.\-]{3,50}?(?:Pvt\.?\s*Ltd|Ltd|Limited|LLP|Inc)[.,]?)",
    ]

    # ── FSSAI ─────────────────────────────────────────────────────────────────
    FSSAI = [
        r"(?:FSSAI|Food\s*Safety|Lic(?:ense)?\s*No\.?|FSSAI\s*Lic\.?\s*No\.?)[:\s]*(\d{14})",
        r"(\d{14})\s*(?:FSSAI|Food\s*Safety)",
    ]

    # ── Consumer Care ─────────────────────────────────────────────────────────
    CONSUMER_CARE = [
        r"(?:Consumer\s*Care|Customer\s*Care|Helpline|Toll\s*Free|"
        r"Contact\s*/?\s*Support|Customer\s*Support)",
    ]

    # Phone: tolerates country codes, brackets, spaces, and dashes
    PHONE = r"(?:(?:\+?91|91|\(?\+?91\)?)[\s\-]?)?[6-9]\d{4}[\s\-]?\d{5}\b"
    TOLLFREE = r"\b1[\s\-]?800[\s\-]?\d{2,3}[\s\-]?\d{3,4}\b"
    EMAIL = r"[\w.\-]+@[\w.\-]+\.\w+"

    # ── "Inclusive of all taxes" (PCR 2011 mandatory alongside MRP) ───────────
    INCLUSIVE_TAX = [
        r"incl(?:usive)?\.?\s*(?:of\s*)?all\s*tax(?:es)?",
        r"all\s*taxes\s*incl(?:uded)?",
        r"incl\.\s*all\s*taxes",
    ]

    # ── Commodity / Generic Name / Product Name (PCR 2011 Rule 6(1)(a)) ───────
    COMMODITY = [
        r"(?:Generic\s*Name\s*(?:of\s*(?:the\s*)?Commodity)?|Common\s*Name|Name\s*of\s*(?:the\s*)?Commodity|Commodity|Product\s*Name|Product|Item)[:\s]*([A-Za-z0-9\s\-_/()&]{2,40}?)(?:[.,;\n]|\b(?:Net|Qty|Quantity|MRP|Mfg|Mfd|PKD|Batch|Model|Brand|Marketed|Country)\b|$)",
    ]

    BRAND = [
        r"(?:Brand\s*Name|Brand)[:\s]*([A-Za-z0-9\s\-_/()&]{2,30}?)(?:[.,;\n]|\b(?:Model|Generic|Net|Qty|MRP|Mfg|PKD)\b|$)",
    ]

    MODEL = [
        r"(?:Model\s*(?:Name|No\.?|ID)?|Model)[:\s]*([A-Za-z0-9\s\-_/()&]{2,30}?)(?:[.,;\n]|\b(?:Colour|Color|Generic|Net|Qty|MRP|Mfg|PKD|Brand)\b|$)",
    ]

    CATEGORY_TAGLINES = [
        r"^audio$", r"^mobile\s*accessories$", r"^pc\s*accessories$", r"^car\s*accessories$",
        r"smart\s*wearables", r"smart\s*gadgets", r"portable\s*brand",
        r"lifestyle", r"make\s*in\s*india", r"warranty",
        r"pure\s*veg", r"100%\s*veg", r"ayurvedic", r"proprietary\s*food"
    ]

    # ── Month lookup ──────────────────────────────────────────────────────────
    MONTHS = {
        "jan": 1, "january": 1,
        "feb": 2, "february": 2,
        "mar": 3, "march": 3,
        "apr": 4, "april": 4,
        "may": 5,
        "jun": 6, "june": 6,
        "jul": 7, "july": 7,
        "aug": 8, "august": 8,
        "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10,
        "nov": 11, "november": 11,
        "dec": 12, "december": 12,
    }


# ============================================================
# EXTRACTOR
# ============================================================

class Extractor:
    def __init__(self):
        self.patterns = Patterns()
        self.config   = Config()

    # ── MRP ──────────────────────────────────────────────────────────────────
    def extract_mrp(self, text: str):
        text = self._clean(text)
        # Replace OCR misreads of ₹
        text = re.sub(r"[%\*#@]\s*(?=\d)", "₹ ", text)

        for pattern in self.patterns.MRP:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            raw_value = match.group(1)
            if self._is_false_positive_mrp(raw_value):
                continue
            try:
                mrp = float(raw_value.replace(",", "."))
                if self.config.MIN_MRP <= mrp <= self.config.MAX_MRP:
                    return mrp
            except ValueError:
                continue
        return None

    def _is_false_positive_mrp(self, value: str) -> bool:
        """Return True if the candidate MRP value matches a known false-positive."""
        for fp_pattern in self.patterns.MRP_FALSE_POSITIVES:
            if re.search(fp_pattern, value.strip(), re.IGNORECASE):
                return True
        return False

    # ── Quantity ──────────────────────────────────────────────────────────────
    def extract_quantity(self, text: str):
        text = self._clean(text)
        for pattern in self.patterns.QTY:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            value = match.group(1)
            unit  = match.group(2).lower() if match.lastindex and match.lastindex >= 2 else ""
            unit  = self._normalize_unit(unit)
            return f"{value} {unit}".strip()
        return None

    def check_unit_violation(self, quantity_str: str):
        """
        Returns the non-standard unit string if PCR 2011 unit rules are violated,
        else returns None.
        """
        if not quantity_str:
            return None
        for bad_unit in self.patterns.NON_STANDARD_UNITS:
            if re.search(r"\b" + re.escape(bad_unit) + r"\b", quantity_str, re.IGNORECASE):
                return bad_unit
        return None

    def _normalize_unit(self, unit: str) -> str:
        """Normalise unit strings to SI standard."""
        u = unit.lower().strip()
        mapping = {
            "ml": "ml", "l": "L", "litre": "L", "liter": "L",
            "g": "g", "gm": "g", "kg": "kg", "mg": "mg",
            "piece": "pcs", "pcs": "pcs", "no": "pcs", "nos": "pcs", "n": "pcs",
            "unit": "pcs", "units": "pcs",
            "tab": "tab", "tabs": "tab", "caps": "caps",
        }
        return mapping.get(u, u)

    # ── Dates ─────────────────────────────────────────────────────────────────
    def extract_mfg_date(self, text: str):
        return self._extract_date(text, self.patterns.MFG)

    def extract_exp_date(self, text: str, mfg_date: dict = None):
        exp = self._extract_date(text, self.patterns.EXP)
        if exp:
            return exp

        # Check shelf-life statement: e.g. "36 Months from Mfg Date" / "use before 36 months of manufacturing"
        for p in self.patterns.SHELF_LIFE:
            m_shelf = re.search(p, text, re.IGNORECASE)
            if m_shelf:
                try:
                    raw_val = int(m_shelf.group(1))
                    # Preserve the OCR value exactly. A legal screening engine must not
                    # silently turn one number into another because it looks more likely.
                    if not (1 <= raw_val <= 240):
                        return {
                            "month": None, "year": None, "needs_review": True,
                            "display": f"OCR read shelf life as {raw_val} months",
                        }
                    months_to_add = raw_val
                    shelf_stmt = f"Use before {months_to_add} months of manufacturing"
                    if mfg_date and "year" in mfg_date and "month" in mfg_date:
                        total_months = mfg_date["year"] * 12 + (mfg_date["month"] - 1) + months_to_add
                        exp_year = total_months // 12
                        exp_month = (total_months % 12) + 1
                        return {
                            "month": exp_month,
                            "year": exp_year,
                            "month_name": exp_month,
                            "shelf_life_statement": shelf_stmt,
                            "inferred_from_shelf_life": f"{months_to_add} Months from Mfg Date",
                            "display": f"{months_to_add} Months from Mfg ({exp_month:02d}/{exp_year})"
                        }
                    else:
                        return {
                            "month": None,
                            "year": None,
                            "shelf_life_statement": shelf_stmt,
                            "inferred_from_shelf_life": f"{months_to_add} Months from Mfg Date",
                            "display": shelf_stmt
                        }
                except Exception:
                    pass
        return None

    def _extract_date(self, text: str, pattern_list: list):
        text = self._clean(text)
        for pattern in pattern_list:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            date_str = match.group(1)
            parsed   = self._parse_date(date_str)
            if parsed:
                return parsed
        return None

    def _parse_date(self, date_str: str):
        """Parse common date formats found on Indian product labels (including dot-matrix misreads)."""
        s = date_str.lower().strip(".,;: ")

        # DD-MMM-YYYY or DD-MMM-YY
        m = re.search(
            r"(\d{1,2})[\s\/\-\.—](jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[\s\/\-\.—](\d{2,4})", s
        )
        if m:
            day, mon, yr = m.groups()
            return self._build_date(int(day), mon, int(yr))

        # MMM-YYYY or MMM-YY  — separator REQUIRED between month and year
        m = re.search(
            r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[\s\/\-\.—](\d{2,4})\b", s
        )
        if m:
            mon, yr = m.groups()
            return self._build_date(None, mon, int(yr))

        # MM/YYYY or MM/YY — with dot-matrix misread tolerance (7, |, I, l as separator)
        m = re.search(r"^(\d{1,2})[\s\/\-\.—7|Il](\d{4})$", s)
        if m:
            mo, yr = int(m.group(1)), int(m.group(2))
            if 1 <= mo <= 12 and 2000 <= yr <= 2099:
                return {"month": mo, "year": yr, "month_name": mo}

        # DD/MM/YYYY (Indian label convention is day-first)
        m = re.search(r"^(\d{1,2})[\s\/\-\.—](\d{1,2})[\s\/\-\.—](\d{2,4})$", s)
        if m:
            day, mo, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if yr < 100:
                yr += 2000
            if 1 <= mo <= 12 and 1 <= day <= 31:
                return {"day": day, "month": mo, "year": yr, "month_name": mo}

        # YYYY-MM-DD (ISO)
        m = re.search(r"(\d{4})[\s\/\-\.—](\d{1,2})[\s\/\-\.—](\d{1,2})", s)
        if m:
            yr, mo, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= mo <= 12:
                return {"day": day, "month": mo, "year": yr, "month_name": mo}

        return None

    def _build_date(self, day, month_abbr: str, year: int):
        mon_abbr = month_abbr[:3] if month_abbr else month_abbr
        month_num = self.patterns.MONTHS.get(mon_abbr)
        if month_num is None:
            return None
        if year < 100:
            year += 2000
        result = {"month": month_num, "year": year, "month_name": month_abbr}
        if day is not None:
            result["day"] = day
        return result

    # ── Company ───────────────────────────────────────────────────────────────
    def extract_company(self, text: str):
        text_clean = self._clean(text)
        for pattern in self.patterns.COMPANY:
            for m in re.finditer(pattern, text_clean, re.IGNORECASE):
                company = m.group(1).strip().rstrip(".,;:")
                company = re.sub(r"^[\s\W]+|[\s\W]+$", "", company)
                # Strip any leading 'Marketed & Customer Care by' or 'by / for / at' prefixes
                company = re.sub(r"^(?:.*?\b(?:by|for|at|bys|by;)\s+)", "", company, flags=re.I)
                company = " ".join(company.split())
                lower_c = company.lower()
                if any(bad in lower_c for bad in ["executive", "deo parfum"]) and "ltd" not in lower_c:
                    continue
                if len(company) >= 4:
                    return company
        return None

    # ── FSSAI ─────────────────────────────────────────────────────────────────
    def extract_fssai(self, text: str):
        text = self._clean(text)
        for pattern in self.patterns.FSSAI:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return m.group(1)
        return None

    # ── Consumer Care ─────────────────────────────────────────────────────────
    def extract_consumer_care(self, text: str):
        text = self._clean(text)
        phone = re.search(self.patterns.PHONE, text) or re.search(self.patterns.TOLLFREE, text)
        email = re.search(self.patterns.EMAIL, text)

        parts = []
        if phone:
            parts.append(phone.group(0).strip())
        if email:
            parts.append(email.group(0))

        return ", ".join(parts) if parts else None

    # ── "Inclusive of all taxes" check (PCR 2011 requirement) ─────────────────
    def check_inclusive_tax(self, text: str) -> bool:
        text = self._clean(text)
        for pattern in self.patterns.INCLUSIVE_TAX:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    # ── Confidence score ──────────────────────────────────────────────────────
    def calculate_confidence(self, results: dict) -> int:
        weights = {
            "mrp":            25,
            "quantity":       20,
            "mfg_date":       15,
            "exp_date":       15,
            "company":        15,
            "inclusive_tax":  10,   # PCR 2011 mandatory
        }
        score = sum(w for field, w in weights.items() if results.get(field))
        if results.get("fssai"):
            score = min(score + 5, 100)
        if results.get("consumer_care"):
            score = min(score + 5, 100)
        return min(score, 100)

    def extract_product_name(self, text: str, top_text: str = ""):
        """
        Extracts product / brand title under Legal Metrology PCR 2011.
        Priority 1: Explicit PCR 2011 declarations ('Generic Name: ...', 'Commodity: ...', 'Brand: ...', 'Model: ...')
        Priority 2: Brand + Common Commodity keywords (e.g. Portronics + Wired Mouse)
        Priority 3: Clean packaging header title lines (excluding marketing category taglines).
        """
        combined_text = text or ""
        
        # ── PRIORITY 1: PCR 2011 Explicit Rule 6(1)(a) Declarations ──────────────
        generic_val = None
        for pat in self.patterns.COMMODITY:
            m = re.search(pat, combined_text, re.IGNORECASE)
            if m:
                val = m.group(1).split("\n")[0].strip().rstrip(".,;:")
                val = re.sub(r"^[\s\W]+|[\s\W]+$", "", val)
                if len(val) >= 3 and not any(re.search(tag, val, re.I) for tag in self.patterns.CATEGORY_TAGLINES):
                    generic_val = val
                    break

        brand_val = None
        for pat in self.patterns.BRAND:
            m = re.search(pat, combined_text, re.IGNORECASE)
            if m:
                val = m.group(1).split("\n")[0].strip().rstrip(".,;:")
                val = re.sub(r"^[\s\W]+|[\s\W]+$", "", val)
                if len(val) >= 2:
                    brand_val = val
                    break

        model_val = None
        for pat in self.patterns.MODEL:
            m = re.search(pat, combined_text, re.IGNORECASE)
            if m:
                val = m.group(1).split("\n")[0].strip().rstrip(".,;:")
                val = re.sub(r"^[\s\W]+|[\s\W]+$", "", val)
                if len(val) >= 2 and not any(re.search(tag, val, re.I) for tag in self.patterns.CATEGORY_TAGLINES):
                    model_val = val
                    break

        # If explicit Generic Name / Commodity declaration is found, assemble full title
        if generic_val:
            parts = []
            if brand_val and brand_val.lower() not in generic_val.lower():
                parts.append(brand_val)
            if model_val and model_val.lower() not in generic_val.lower():
                parts.append(model_val)
            parts.append(generic_val)
            return " ".join(parts)

        # ── PRIORITY 2: Prominent Brand + Hardware / Commodity Keywords ─────────
        known_commodities = [
            r"wired\s*mouse", r"wireless\s*mouse", r"optical\s*mouse", r"\bmouse\b",
            r"mechanical\s*keyboard", r"wireless\s*keyboard", r"\bkeyboard\b",
            r"earbuds", r"earphones", r"headphones", r"neckband",
            r"power\s*bank", r"fast\s*charger", r"\bcharger\b", r"type-?c\s*cable", r"\bcable\b",
            r"smart\s*watch", r"bluetooth\s*speaker", r"\bspeaker\b",
            r"gluco\s*biscuits?", r"\bbiscuits?\b", r"\bcookies?\b", r"\bnoodles?\b",
            r"sunflower\s*oil", r"mustard\s*oil", r"\bedible\s*oil\b", r"\boil\b",
            r"chakki\s*atta", r"\batta\b", r"\bshampoo\b", r"\bsoap\b", r"eau\s*de\s*parfum",
            r"\bperfume\b", r"\bdeodorant\b"
        ]
        
        detected_commodity = None
        for c_pat in known_commodities:
            m = re.search(c_pat, combined_text, re.IGNORECASE)
            if m:
                detected_commodity = m.group(0).title()
                break

        if detected_commodity:
            if brand_val:
                return f"{brand_val} {detected_commodity}"
            company_hint = self.extract_company(combined_text)
            if company_hint:
                first_word = company_hint.split()[0]
                if len(first_word) >= 3 and first_word.lower() not in ["the", "m/s", "idam", "private", "pvt"]:
                    return f"{first_word} {detected_commodity}"
            return detected_commodity

        # ── PRIORITY 3: Clean Packaging Header Lines ────────────────────────────
        source = top_text if top_text and len(top_text.strip()) > 3 else text
        lines = [line.strip() for line in source.split("\n") if line.strip()]
        
        skip_patterns = [
            r"^(?:mfg|manufactured|marketed|packed|distributed|imported|lic|fssai|regd|batch|lot|mrp|rs|net|exp|use before|ingredients|directions|caution|store in|keep out)\b",
            r"(?:pvt\.?\s*ltd|ltd\.?|limited|llp|inc\.)",
            r"^\d+[\s\w]*$",
            r"^[\W_]+$"
        ] + self.patterns.CATEGORY_TAGLINES
        
        candidates = []
        for line in lines:
            if any(re.search(pat, line, re.IGNORECASE) for pat in skip_patterns):
                continue
            
            cleaned = re.sub(r"^[^\w]+|[^\w]+$", "", line).strip()
            if any(re.search(tag, cleaned, re.IGNORECASE) for tag in self.patterns.CATEGORY_TAGLINES):
                continue

            if len(cleaned) >= 3 and not re.match(r"^\d+$", cleaned):
                candidates.append(cleaned)
                if len(candidates) >= 2:
                    break
                    
        if candidates:
            return " ".join(candidates)
        return None

    # ── Helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()


# ============================================================
# IMAGE PREPROCESSOR
# ============================================================

class Preprocessor:
    def __init__(self, config=None):
        self.config = config or Config()

    # ── Accept either a file path (str/Path) or raw bytes ─────────────────────
    def _load_image(self, source):
        if isinstance(source, (str, Path)):
            img = cv2.imread(str(source))
            if img is None:
                raise ValueError(f"Could not load image: {source}")
            return img
        # bytes / numpy array from FastAPI upload
        nparr = np.frombuffer(source, np.uint8)
        img   = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Could not decode image bytes")
        
        # Clamp large camera images (e.g. 12MP/24MP) to max 1600px for 5x faster processing
        h, w = img.shape[:2]
        if max(h, w) > 1600:
            scale = 1600.0 / max(h, w)
            img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        return img

    def preprocess_full(self, source):
        """Full image — adaptive threshold, PSM 11 (sparse text)."""
        img  = self._load_image(source)
        img  = self.detect_rotation(img)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        h, w = gray.shape
        if w < self.config.MIN_IMAGE_WIDTH:
            scale = self.config.MIN_IMAGE_WIDTH / w
            gray  = cv2.resize(gray, None, fx=scale, fy=scale,
                               interpolation=cv2.INTER_CUBIC)

        # Fast edge-preserving filter (0.05s instead of fastNlMeans 10.0s)
        denoised = cv2.bilateralFilter(gray, 7, 50, 50)
        thresh = cv2.adaptiveThreshold(
            denoised, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            blockSize=11, C=2
        )
        return thresh, img

    def preprocess_bottom_zone(self, source):
        """Bottom zone — heavy contrast enhancement for small MRP / date text."""
        img         = self._load_image(source)
        h, w        = img.shape[:2]
        bottom      = img[int(h * self.config.BOTTOM_ZONE_START):, :]
        # Moderate 1.5x scaling preserves small-text fidelity without memory/time bloat
        scaled      = cv2.resize(bottom, None, fx=1.5, fy=1.5,
                                 interpolation=cv2.INTER_CUBIC)
        gray        = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
        clahe       = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8))
        contrast    = clahe.apply(gray)
        kernel      = np.array([[-1, -1, -1], [-1, 9, -1], [-1, -1, -1]])
        sharpened   = cv2.filter2D(contrast, -1, kernel)
        _, thresh   = cv2.threshold(sharpened, 0, 255,
                                    cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return thresh, bottom

    def preprocess_top_zone(self, source):
        """Top zone — fast bilateral processing for brand/model names."""
        img        = self._load_image(source)
        h, w       = img.shape[:2]
        top        = img[:int(h * self.config.TOP_ZONE_END), :]
        gray       = cv2.cvtColor(top, cv2.COLOR_BGR2GRAY)
        denoised   = cv2.bilateralFilter(gray, 5, 40, 40)
        _, thresh  = cv2.threshold(denoised, 0, 255,
                                   cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return thresh, top

    def detect_rotation(self, image):
        """Detect and correct skew via Hough lines."""
        gray  = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        edges = cv2.Canny(gray, 50, 150)
        lines = cv2.HoughLines(edges, 1, np.pi / 180, threshold=100)

        if lines is None or len(lines) == 0:
            return image

        angles = []
        for line in lines[:30]:
            rho, theta = line[0]
            angle = np.degrees(theta) - 90
            if -45 < angle < 45:
                angles.append(angle)

        if not angles or abs(np.median(angles)) < 0.5:
            return image

        avg_angle  = np.median(angles)
        h, w       = image.shape[:2]
        center     = (w // 2, h // 2)
        matrix     = cv2.getRotationMatrix2D(center, avg_angle, 1.0)
        rotated    = cv2.warpAffine(image, matrix, (w, h),
                                    borderMode=cv2.BORDER_REPLICATE)
        return rotated


# ============================================================
# OCR ENGINE
# ============================================================

_SHARED_PADDLE = None
_SHARED_PADDLE_MODE = "rapidocr"
_SHARED_PADDLE_CHECKED = False

class OCREngine:
    """
    Hybrid Multi-Engine OCR:
    - Primary Ensemble: RapidOCR/ONNX (PaddleOCR-derived models) + Tesseract (structured multi-pass zones)
    - Execution Order:
        1. PaddleOCR pass (if available on the platform)
        2. Tesseract multi-pass (full sparse PSM 11, bottom CLAHE PSM 6, top PSM 7, raw PSM 3)
        3. Line-level deduplication & merge across both engines
        4. Tolerant PCR-2011 rule extraction
    - Resilient Failover: If Paddle native DLL fails to load, gracefully falls back to Tesseract.
    """
    def __init__(self):
        self.config          = Config()
        self.extractor       = Extractor()
        self.preprocessor    = Preprocessor(self.config)

    def _get_paddle(self):
        global _SHARED_PADDLE, _SHARED_PADDLE_MODE, _SHARED_PADDLE_CHECKED
        if _SHARED_PADDLE_CHECKED:
            self._paddle_mode = _SHARED_PADDLE_MODE
            return _SHARED_PADDLE
        _SHARED_PADDLE_CHECKED = True
        # 1. Try the maintained RapidOCR package with ONNX Runtime.
        #    rapidocr_onnxruntime is legacy and does not support Python 3.13.
        try:
            from rapidocr import RapidOCR
            _SHARED_PADDLE = RapidOCR()
            _SHARED_PADDLE_MODE = "rapidocr_modern"
            self._paddle_mode = "rapidocr_modern"
            print("[OCR Engine] RapidOCR + ONNX Runtime warmed up and cached in memory.")
            return _SHARED_PADDLE
        except Exception as e1:
            # 2. Backward-compatible support for older deployments.
            try:
                from rapidocr_onnxruntime import RapidOCR
                _SHARED_PADDLE = RapidOCR()
                _SHARED_PADDLE_MODE = "rapidocr_legacy"
                self._paddle_mode = "rapidocr_legacy"
                print("[OCR Engine] Legacy RapidOCR ONNX engine warmed up and cached in memory.")
                return _SHARED_PADDLE
            except Exception as e_legacy:
                # 3. Try native PaddleOCR if explicitly installed.
                try:
                    import sys
                    if sys.platform == "win32":
                        import ctypes
                        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
                    from paddleocr import PaddleOCR
                    _SHARED_PADDLE = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
                    _SHARED_PADDLE_MODE = "paddleocr"
                    self._paddle_mode = "paddleocr"
                    print("[OCR Engine] PaddleOCR native warmed up and cached in memory.")
                    return _SHARED_PADDLE
                except Exception as e2:
                    print(
                        "[OCR Engine] RapidOCR/PaddleOCR unavailable "
                        f"(rapidocr={e1}; legacy={e_legacy}; paddle={e2}). "
                        "Using enhanced multi-pass Tesseract."
                    )
                    _SHARED_PADDLE = None
                    return None

    def ocr_paddle(self, source) -> str:
        paddle = self._get_paddle()
        if not paddle:
            return ""
        try:
            img = self.preprocessor._load_image(source)
            # Optimize image resolution for RapidOCR ONNX inference (1024px is optimal for deep learning OCR)
            h, w = img.shape[:2]
            if max(h, w) > 1024:
                scale = 1024.0 / max(h, w)
                img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            mode = getattr(self, "_paddle_mode", "")
            if mode == "rapidocr_modern":
                result = paddle(img)
                # rapidocr>=3 returns RapidOCROutput with txts/scores/boxes.
                texts = getattr(result, "txts", None)
                if texts is None:
                    return ""
                return "\n".join(str(text).strip() for text in texts if str(text).strip())
            elif mode == "rapidocr_legacy":
                result, _ = paddle(img)
                lines = []
                if result:
                    for item in result:
                        if len(item) >= 2 and item[1]:
                            text = str(item[1]).strip()
                            if text:
                                lines.append(text)
                return "\n".join(lines)
            else:
                result = paddle.ocr(img, cls=True)
                lines = []
                if result and result[0]:
                    for line in result[0]:
                        if line and len(line) >= 2 and line[1]:
                            text = str(line[1][0]).strip()
                            if text:
                                lines.append(text)
                return "\n".join(lines)
        except Exception as e:
            print(f"[OCR Engine] PaddleOCR execution error: {e}")
            return ""

    def _run_tesseract(self, image, psm: int) -> str:
        """Run Tesseract with OEM 3 (LSTM) and specified PSM."""
        custom_config = f"--oem 3 --psm {psm}"
        return pytesseract.image_to_string(image, config=custom_config)

    def ocr_full(self, source) -> str:
        """PSM 11 — sparse text, best for full mixed-layout labels."""
        preprocessed, _ = self.preprocessor.preprocess_full(source)
        return self._run_tesseract(preprocessed, psm=11)

    def ocr_bottom(self, source) -> str:
        """PSM 6 — uniform block; bottom zone is usually dense small text."""
        preprocessed, _ = self.preprocessor.preprocess_bottom_zone(source)
        return self._run_tesseract(preprocessed, psm=6)

    def ocr_top(self, source) -> str:
        """PSM 7 — single line; top is usually brand name."""
        preprocessed, _ = self.preprocessor.preprocess_top_zone(source)
        return self._run_tesseract(preprocessed, psm=7)

    def ocr_raw(self, source) -> str:
        """PSM 3 — fully automatic on unprocessed image as a fallback."""
        img  = self.preprocessor._load_image(source)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return self._run_tesseract(gray, psm=3)

    @staticmethod
    def merge_texts(*texts) -> str:
        """Deduplicate lines across multiple OCR passes."""
        seen, lines = set(), []
        for text in texts:
            if not text:
                continue
            for line in text.split("\n"):
                line      = line.strip()
                line_low  = line.lower()
                if line and line_low not in seen and len(line) > 1:
                    seen.add(line_low)
                    lines.append(line)
        return "\n".join(lines)

    def extract_all(self, source) -> dict:
        """
        Full hybrid extraction pipeline with parallel engine execution.
        `source` can be a file path (str / Path) or raw image bytes.
        """
        from concurrent.futures import ThreadPoolExecutor

        def _run_tess_passes():
            f_text = self.ocr_full(source)
            b_text = self.ocr_bottom(source)
            t_text = self.ocr_top(source)
            r_text = self.ocr_raw(source)
            return f_text, b_text, t_text, r_text

        # Run RapidOCR/ONNX (or Paddle fallback) and Tesseract concurrently across CPU cores
        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_paddle = executor.submit(self.ocr_paddle, source)
            fut_tess   = executor.submit(_run_tess_passes)

            paddle_text = fut_paddle.result()
            full_text, bottom_text, top_text, raw_text = fut_tess.result()

        combined = self.merge_texts(paddle_text, full_text, bottom_text, top_text, raw_text)

        ex = self.extractor
        quantity = ex.extract_quantity(combined)
        mfg_date = ex.extract_mfg_date(combined)
        exp_date = ex.extract_exp_date(combined, mfg_date)

        product_name = ex.extract_product_name(combined, top_text=top_text)

        results = {
            "product_name":     product_name,
            "mrp":              ex.extract_mrp(combined),
            "quantity":         quantity,
            "mfg_date":         mfg_date,
            "exp_date":         exp_date,
            "company":          ex.extract_company(combined),
            "fssai":            ex.extract_fssai(combined),
            "consumer_care":    ex.extract_consumer_care(combined),
            "inclusive_tax":    ex.check_inclusive_tax(combined),
            "unit_violation":   ex.check_unit_violation(quantity),
            "raw_text":         combined,
            "lines":            [l.strip() for l in combined.split("\n") if l.strip()],
            "engines_used":     {
                "rapidocr_onnx": bool(_SHARED_PADDLE is not None and _SHARED_PADDLE_MODE.startswith("rapidocr")),
                "paddle_ocr": bool(_SHARED_PADDLE is not None and _SHARED_PADDLE_MODE == "paddleocr"),
                "tesseract": True,
            },
        }
        results["confidence"] = ex.calculate_confidence(results)
        return results


# ============================================================
# VALIDATOR  (PCR 2011 compliance rules)
# ============================================================

class Validator:

    @staticmethod
    def validate_mrp(mrp, inclusive_tax: bool):
        errors = []
        if mrp is None:
            errors.append("MRP not found — mandatory under PCR 2011")
        elif not (Config.MIN_MRP <= mrp <= Config.MAX_MRP):
            errors.append(f"MRP ₹{mrp} is outside valid range")
        if not inclusive_tax:
            errors.append("'Inclusive of all taxes' declaration missing — mandatory under PCR 2011")
        return errors

    @staticmethod
    def validate_dates(mfg_date, exp_date):
        errors = []
        if mfg_date is None:
            errors.append("Manufacturing date not found — mandatory under PCR 2011")
        if exp_date and exp_date.get("year") and exp_date.get("month"):
            now = datetime.now()
            try:
                exp_dt = datetime(exp_date["year"], exp_date["month"], 1)
                if exp_dt < now:
                    errors.append(f"Product may be expired ({exp_date.get('month_name', exp_date['month'])} {exp_date['year']})")
            except (ValueError, KeyError):
                pass

        if mfg_date and exp_date and mfg_date.get("year") and exp_date.get("year"):
            mfg_yr, mfg_mo = mfg_date.get("year", 0), mfg_date.get("month", 0)
            exp_yr, exp_mo = exp_date.get("year", 0), exp_date.get("month", 0)
            if (mfg_yr, mfg_mo) > (exp_yr, exp_mo):
                errors.append("Manufacturing date is after expiry date")
        return errors

    @staticmethod
    def validate_quantity(quantity, unit_violation):
        errors = []
        if quantity is None:
            errors.append("Net quantity not found — mandatory under PCR 2011")
        if unit_violation:
            errors.append(
                f"Non-standard unit '{unit_violation}' used — PCR 2011 requires SI units"
            )
        return errors

    @staticmethod
    def validate_company(company):
        if not company:
            return ["Manufacturer / packer name not found — mandatory under PCR 2011"]
        return []

    @classmethod
    def validate_all(cls, results: dict) -> dict:
        errors = []
        errors += cls.validate_mrp(results.get("mrp"), results.get("inclusive_tax", False))
        errors += cls.validate_dates(results.get("mfg_date"), results.get("exp_date"))
        errors += cls.validate_quantity(results.get("quantity"), results.get("unit_violation"))
        errors += cls.validate_company(results.get("company"))

        return {
            "is_compliant": len(errors) == 0,
            "violations":   errors,
        }


# ============================================================
# PUBLIC API  (used by main.py / FastAPI)
# ============================================================

class LabelOCR:
    """High-level scanner — accepts file path or raw image bytes."""

    def __init__(self, source):
        """
        Args:
            source: file path (str / Path) OR raw image bytes from an upload
        """
        self.source = source
        self.engine = OCREngine()

    def scan(self) -> dict:
        results              = self.engine.extract_all(self.source)
        results["pcr_2011"]  = Validator.validate_all(results)
        return results

    def scan_without_raw(self) -> dict:
        """Same as scan() but strips raw_text — suitable for API responses."""
        results = self.scan()
        results.pop("raw_text", None)
        return results

    def to_json(self) -> str:
        return json.dumps(self.scan(), indent=2, default=str)

    # ── CLI helper ─────────────────────────────────────────────────────────────
    def format_report(self) -> str:
        r = self.scan()
        sep = "=" * 55
        lines = [
            sep, "TOLMAAP — PRODUCT LABEL OCR RESULTS", sep,
            f"MRP:            ₹{r.get('mrp', 'Not found')}",
            f"Incl. taxes:    {'Yes ✓' if r.get('inclusive_tax') else 'No ✗'}",
            f"Quantity:       {r.get('quantity', 'Not found')}",
            f"Unit violation: {r.get('unit_violation') or 'None'}",
            f"Mfg Date:       {self._fmt_date(r.get('mfg_date'))}",
            f"Exp Date:       {self._fmt_date(r.get('exp_date'))}",
            f"Company:        {r.get('company', 'Not found')}",
            f"FSSAI:          {r.get('fssai', 'Not found')}",
            f"Consumer Care:  {r.get('consumer_care', 'Not found')}",
            "-" * 55,
            f"Confidence:     {r.get('confidence', 0)}%",
            f"PCR Compliant:  {'YES ✓' if r['pcr_2011']['is_compliant'] else 'NO ✗'}",
        ]
        if r["pcr_2011"]["violations"]:
            lines.append("Violations:")
            for v in r["pcr_2011"]["violations"]:
                lines.append(f"  • {v}")
        lines.append(sep)
        return "\n".join(lines)

    @staticmethod
    def _fmt_date(d) -> str:
        if d is None:
            return "Not found"
        mn = d.get("month_name", "")
        if isinstance(mn, int):
            mn = datetime(2000, mn, 1).strftime("%B").lower()
        return f"{mn} {d.get('year', '')}".strip()


# ============================================================
# CLI ENTRY POINT
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Tolmaap Label OCR — check PCR 2011 compliance"
    )
    parser.add_argument("image", help="Path to product label image")
    parser.add_argument("-j", "--json",    action="store_true", help="Output as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="Include raw OCR text")
    parser.add_argument("-o", "--output",  help="Save output to file")
    args = parser.parse_args()

    try:
        scanner = LabelOCR(args.image)

        if args.json:
            output = scanner.to_json()
            if args.verbose:
                pass  # raw_text already included in to_json()
            else:
                data = json.loads(output)
                data.pop("raw_text", None)
                output = json.dumps(data, indent=2, default=str)
        else:
            output = scanner.format_report()
            if args.verbose:
                results = scanner.scan()
                output += "\n\n--- RAW OCR TEXT ---\n" + results.get("raw_text", "")

        if args.output:
            Path(args.output).write_text(output, encoding="utf-8")
            print(f"Saved to {args.output}")
        else:
            print(output)

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    main()
