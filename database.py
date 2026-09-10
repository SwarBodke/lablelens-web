"""Persistence layer for LableLens.

Design goals for the LableLens platform:
- every inspection is scoped to the signed workspace/session that created it
- guest rows carry the session expiry so temporary workspaces can be purged
- OCR guesses never auto-teach the verified product catalogue
- six grouped LMPC checks and tri-state outcomes are stored as evidence
- each session has a SHA-256 record chain (tamper-evident, not externally notarised)
- existing SQLite/PostgreSQL databases are migrated by adding missing columns
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from evidence import chained_record_hash

DB_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SQLITE_PATH = os.getenv("SQLITE_PATH") or os.path.join(DB_DIR, "lablelens_history.db")
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")

_use_postgres = False
if DATABASE_URL and DATABASE_URL.startswith(("postgresql://", "postgres://")):
    try:
        import psycopg2  # noqa: F401
        _use_postgres = True
    except Exception:
        print("[DB] psycopg2 unavailable; using SQLite")


def _get_connection():
    if _use_postgres and DATABASE_URL:
        try:
            import psycopg2
            return psycopg2.connect(DATABASE_URL), "postgres"
        except Exception as exc:
            print(f"[DB] PostgreSQL unavailable ({exc}); using SQLite")
    import sqlite3
    conn = sqlite3.connect(DEFAULT_SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn, "sqlite"


INSPECTION_COLUMNS = {
    "session_id": "TEXT",
    "session_expires_at": "BIGINT",
    "user_label": "TEXT",
    "actor_id": "TEXT",
    "role": "TEXT DEFAULT 'guest'",
    "source_type": "TEXT DEFAULT 'physical'",
    "evidence_scope": "TEXT DEFAULT 'partial'",
    "category": "TEXT DEFAULT 'general'",
    "manufacturer": "TEXT",
    "country_of_origin": "TEXT",
    "generic_name": "TEXT",
    "quantity_details": "TEXT",
    "fssai": "TEXT",
    "ocr_confidence": "REAL",
    "extraction_completeness": "INTEGER DEFAULT 0",
    "compliance_status": "TEXT DEFAULT 'needs_review'",
    "checks": "TEXT",
    "needs_review": "TEXT",
    "evidence": "TEXT",
    "codes": "TEXT",
    "ruleset_id": "TEXT",
    "engine_version": "TEXT",
    "ocr_route": "TEXT",
    "processing_time_ms": "REAL",
    "previous_hash": "TEXT",
    "record_hash": "TEXT",
    "audit_payload": "TEXT",
}

CATALOG_COLUMNS = {
    "verified": "INTEGER DEFAULT 0",
    "provenance": "TEXT DEFAULT 'unverified'",
    "confirmed_by_session": "TEXT",
}


def _add_missing_columns(conn, db_type: str, table: str, columns: Dict[str, str]):
    cur = conn.cursor()
    if db_type == "sqlite":
        cur.execute(f"PRAGMA table_info({table});")
        existing = {row[1] for row in cur.fetchall()}
        for name, decl in columns.items():
            if name not in existing:
                # SQLite is dynamically typed; BIGINT is accepted as affinity.
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl};")
    else:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
        existing = {r[0] for r in cur.fetchall()}
        pg_decl = {
            "REAL": "DOUBLE PRECISION",
            "REAL DEFAULT 0": "DOUBLE PRECISION DEFAULT 0",
            "INTEGER DEFAULT 0": "INTEGER DEFAULT 0",
            "BIGINT": "BIGINT",
        }
        for name, decl in columns.items():
            if name not in existing:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {pg_decl.get(decl, decl)};")



def _init_management_tables(conn, db_type: str) -> None:
    """Create additive management tables. Existing inspection/catalog rows are untouched."""
    cur = conn.cursor()
    if db_type == "postgres":
        cur.execute("""
            CREATE TABLE IF NOT EXISTS actors (
                actor_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                last_login TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS complaints (
                id SERIAL PRIMARY KEY,
                inspection_id INTEGER NOT NULL,
                created_by_actor TEXT NOT NULL,
                created_by_label TEXT,
                case_type TEXT DEFAULT 'compliance_case',
                priority TEXT DEFAULT 'normal',
                status TEXT DEFAULT 'draft',
                officer_remarks TEXT,
                admin_notes TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS complaint_violations (
                id SERIAL PRIMARY KEY,
                complaint_id INTEGER NOT NULL,
                violation_type TEXT,
                description TEXT NOT NULL,
                rule_reference TEXT,
                evidence_text TEXT
            );
            CREATE TABLE IF NOT EXISTS complaint_status_history (
                id SERIAL PRIMARY KEY,
                complaint_id INTEGER NOT NULL,
                old_status TEXT,
                new_status TEXT NOT NULL,
                actor_id TEXT,
                actor_label TEXT,
                notes TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id SERIAL PRIMARY KEY,
                actor_id TEXT,
                actor_label TEXT,
                actor_role TEXT,
                action TEXT NOT NULL,
                entity_type TEXT,
                entity_id TEXT,
                details TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_inspections_actor ON inspections(actor_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_inspections_created ON inspections(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_inspections_status ON inspections(compliance_status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_complaints_status ON complaints(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_complaints_inspection ON complaints(inspection_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at)")
    else:
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS actors (
                actor_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                last_login TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inspection_id INTEGER NOT NULL,
                created_by_actor TEXT NOT NULL,
                created_by_label TEXT,
                case_type TEXT DEFAULT 'compliance_case',
                priority TEXT DEFAULT 'normal',
                status TEXT DEFAULT 'draft',
                officer_remarks TEXT,
                admin_notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (inspection_id) REFERENCES inspections(id)
            );
            CREATE TABLE IF NOT EXISTS complaint_violations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                complaint_id INTEGER NOT NULL,
                violation_type TEXT,
                description TEXT NOT NULL,
                rule_reference TEXT,
                evidence_text TEXT,
                FOREIGN KEY (complaint_id) REFERENCES complaints(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS complaint_status_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                complaint_id INTEGER NOT NULL,
                old_status TEXT,
                new_status TEXT NOT NULL,
                actor_id TEXT,
                actor_label TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (complaint_id) REFERENCES complaints(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id TEXT,
                actor_label TEXT,
                actor_role TEXT,
                action TEXT NOT NULL,
                entity_type TEXT,
                entity_id TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_inspections_actor ON inspections(actor_id);
            CREATE INDEX IF NOT EXISTS idx_inspections_created ON inspections(created_at);
            CREATE INDEX IF NOT EXISTS idx_inspections_status ON inspections(compliance_status);
            CREATE INDEX IF NOT EXISTS idx_complaints_status ON complaints(status);
            CREATE INDEX IF NOT EXISTS idx_complaints_inspection ON complaints(inspection_id);
            CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at);
        """)

def init_db():
    conn, db_type = _get_connection()
    try:
        cur = conn.cursor()
        if db_type == "postgres":
            cur.execute("""
                CREATE TABLE IF NOT EXISTS inspections (
                    id SERIAL PRIMARY KEY,
                    product_name VARCHAR(255),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    company TEXT,
                    quantity VARCHAR(100),
                    mrp NUMERIC(12,2),
                    inclusive_tax BOOLEAN DEFAULT FALSE,
                    mfg_date TEXT,
                    exp_date TEXT,
                    consumer_care TEXT,
                    confidence INTEGER DEFAULT 0,
                    is_compliant BOOLEAN DEFAULT FALSE,
                    violations TEXT,
                    raw_text TEXT,
                    status VARCHAR(50) DEFAULT 'needs_review'
                );
                CREATE TABLE IF NOT EXISTS products_catalog (
                    id SERIAL PRIMARY KEY,
                    product_name VARCHAR(255) NOT NULL,
                    company TEXT,
                    quantity VARCHAR(100),
                    fssai VARCHAR(50),
                    mrp NUMERIC(12,2),
                    times_inspected INTEGER DEFAULT 1,
                    last_inspected TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS inspections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    company TEXT,
                    quantity TEXT,
                    mrp REAL,
                    inclusive_tax INTEGER DEFAULT 0,
                    mfg_date TEXT,
                    exp_date TEXT,
                    consumer_care TEXT,
                    confidence INTEGER DEFAULT 0,
                    is_compliant INTEGER DEFAULT 0,
                    violations TEXT,
                    raw_text TEXT,
                    status TEXT DEFAULT 'needs_review'
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS products_catalog (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_name TEXT NOT NULL,
                    company TEXT,
                    quantity TEXT,
                    fssai TEXT,
                    mrp REAL,
                    times_inspected INTEGER DEFAULT 1,
                    last_inspected TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
        _add_missing_columns(conn, db_type, "inspections", INSPECTION_COLUMNS)
        _add_missing_columns(conn, db_type, "products_catalog", CATALOG_COLUMNS)
        _init_management_tables(conn, db_type)
        conn.commit()
    finally:
        conn.close()


def _dict_cursor(conn, db_type: str):
    if db_type == "postgres":
        from psycopg2.extras import RealDictCursor
        return conn.cursor(cursor_factory=RealDictCursor)
    return conn.cursor()


def _json(v: Any) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return str(v) if v not in (None, "") else ""


def _parse_json_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    for field in [
        "manufacturer", "quantity_details", "mfg_date", "exp_date", "consumer_care",
        "checks", "violations", "needs_review", "evidence", "codes", "audit_payload",
    ]:
        val = item.get(field)
        if isinstance(val, str) and val.strip()[:1] in {"{", "["}:
            try:
                item[field] = json.loads(val)
            except Exception:
                pass
    if "is_compliant" in item:
        item["is_compliant"] = bool(item["is_compliant"])
    if "inclusive_tax" in item:
        item["inclusive_tax"] = bool(item["inclusive_tax"])
    if item.get("record_hash"):
        item["audit"] = {
            "algorithm": "SHA-256",
            "chain_scope": "session",
            "previous_hash": item.get("previous_hash") or None,
            "record_hash": item.get("record_hash"),
        }
    return item


def _audit_payload(
    data: Dict[str, Any],
    session: Dict[str, Any],
    product_name: str,
    created_at: str,
    evidence: List[Dict[str, Any]],
    status: str,
) -> Dict[str, Any]:
    # Local filesystem paths intentionally do not enter the audit digest.
    evidence_public = [{k: v for k, v in x.items() if k != "path"} for x in evidence]
    finding = data.get("lmpc") or data.get("pcr_2011") or {}
    return {
        "schema": "lablelens-inspection-audit-v1",
        "session_id": session.get("sid"),
        "role": session.get("role", "guest"),
        "created_at": created_at,
        "product_name": product_name,
        "source_type": data.get("source_type", "physical"),
        "evidence_scope": data.get("evidence_scope", "partial"),
        "category": data.get("category", "general"),
        "manufacturer": data.get("manufacturer"),
        "country_of_origin": data.get("country_of_origin"),
        "generic_name": data.get("generic_name"),
        "quantity_details": data.get("quantity_details"),
        "mrp": data.get("mrp"),
        "mfg_date": data.get("mfg_date"),
        "exp_date": data.get("exp_date"),
        "consumer_care": data.get("consumer_care"),
        "checks": data.get("checks") or {},
        "violations": finding.get("violations") or [],
        "needs_review": finding.get("needs_review") or [],
        "evidence": evidence_public,
        "codes": data.get("codes") or [],
        "raw_text": data.get("raw_text") or "",
        "ruleset_id": data.get("ruleset_id"),
        "engine_version": data.get("engine_version") or "2.2.0",
        "status": status,
    }



def _inspection_ocr_route(data: Dict[str, Any]) -> Optional[str]:
    engines = data.get("engines_used") or {}
    routes = engines.get("routes_by_panel") or []
    if routes:
        unique = [str(x) for x in routes if x]
        if not unique:
            return None
        if len(set(unique)) == 1:
            return unique[0]
        if any("rapid" in x for x in unique):
            return "mixed_tesseract_rapid"
        return "mixed_tesseract"
    return engines.get("route") or ("text_input" if engines.get("text_input") else None)


def _inspection_processing_ms(data: Dict[str, Any]) -> Optional[float]:
    value = data.get("ocr_timing_ms")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        try:
            return float(value.get("total"))
        except (TypeError, ValueError):
            return None
    return None

def save_inspection(
    data: Dict[str, Any],
    session: Dict[str, Any],
    product_name: Optional[str] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Persist one inspection and append it to the workspace hash chain."""
    init_db()
    conn, db_type = _get_connection()
    finding = data.get("lmpc") or data.get("pcr_2011") or {}
    status = data.get("compliance_status") or finding.get("status") or "needs_review"
    is_compliant = status == "compliant"
    final_name = product_name or data.get("product_name") or "Unnamed product"
    evidence_rows = evidence or data.get("evidence") or []
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    try:
        cur = conn.cursor()
        ph = "%s" if db_type == "postgres" else "?"
        cur.execute(
            f"SELECT record_hash FROM inspections WHERE session_id={ph} AND record_hash IS NOT NULL "
            f"ORDER BY id DESC LIMIT 1",
            (session.get("sid"),),
        )
        previous_row = cur.fetchone()
        previous_hash = previous_row[0] if previous_row else None
        audit_payload = _audit_payload(data, session, final_name, created_at, evidence_rows, status)
        record_hash = chained_record_hash(previous_hash, audit_payload)
        data["audit"] = {
            "algorithm": "SHA-256",
            "chain_scope": "session",
            "previous_hash": previous_hash,
            "record_hash": record_hash,
        }

        fields = [
            "product_name", "created_at", "session_id", "session_expires_at", "user_label", "actor_id", "role",
            "source_type", "evidence_scope", "category", "company", "manufacturer", "country_of_origin",
            "generic_name", "quantity", "quantity_details", "mrp", "inclusive_tax", "mfg_date", "exp_date",
            "consumer_care", "fssai", "confidence", "ocr_confidence", "extraction_completeness",
            "is_compliant", "compliance_status", "violations", "needs_review", "checks", "raw_text",
            "evidence", "codes", "ruleset_id", "engine_version", "ocr_route", "processing_time_ms", "previous_hash", "record_hash", "audit_payload", "status",
        ]
        ocr_conf = data.get("ocr_confidence")
        values = [
            final_name, created_at, session.get("sid"), session.get("exp"), session.get("label"), session.get("actor_id"), session.get("role", "guest"),
            data.get("source_type", "physical"), data.get("evidence_scope", "partial"), data.get("category", "general"),
            data.get("company"), _json(data.get("manufacturer")), data.get("country_of_origin"), data.get("generic_name"),
            data.get("quantity"), _json(data.get("quantity_details")),
            float(data["mrp"]) if data.get("mrp") is not None else None,
            bool(data.get("inclusive_tax")), _json(data.get("mfg_date")), _json(data.get("exp_date")),
            _json(data.get("consumer_care")), data.get("fssai"),
            int(round(float(ocr_conf or 0))), float(ocr_conf) if ocr_conf is not None else None,
            int(data.get("extraction_completeness", 0) or 0), is_compliant, status,
            _json(finding.get("violations") or []), _json(finding.get("needs_review") or []),
            _json(data.get("checks") or {}), data.get("raw_text") or "", _json(evidence_rows),
            _json(data.get("codes") or []), data.get("ruleset_id"), data.get("engine_version") or "2.2.0",
            _inspection_ocr_route(data), _inspection_processing_ms(data), previous_hash, record_hash, _json(audit_payload), status,
        ]

        cols = ",".join(fields)
        placeholders = ",".join([ph] * len(fields))
        if db_type == "postgres":
            cur.execute(f"INSERT INTO inspections ({cols}) VALUES ({placeholders}) RETURNING id", tuple(values))
            new_id = int(cur.fetchone()[0])
        else:
            sqlite_values = tuple(1 if v is True else 0 if v is False else v for v in values)
            cur.execute(f"INSERT INTO inspections ({cols}) VALUES ({placeholders})", sqlite_values)
            new_id = int(cur.lastrowid)
        conn.commit()
        return new_id
    finally:
        conn.close()


def get_inspections(session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    init_db()
    conn, db_type = _get_connection()
    try:
        cur = _dict_cursor(conn, db_type)
        ph = "%s" if db_type == "postgres" else "?"
        cur.execute(
            f"SELECT * FROM inspections WHERE session_id = {ph} ORDER BY id DESC LIMIT {ph}",
            (session_id, min(max(int(limit), 1), 200)),
        )
        return [_parse_json_fields(dict(r)) for r in cur.fetchall()]
    finally:
        conn.close()


def get_inspection_by_id(inspection_id: int, session_id: str) -> Optional[Dict[str, Any]]:
    init_db()
    conn, db_type = _get_connection()
    try:
        cur = _dict_cursor(conn, db_type)
        ph = "%s" if db_type == "postgres" else "?"
        cur.execute(f"SELECT * FROM inspections WHERE id={ph} AND session_id={ph}", (inspection_id, session_id))
        row = cur.fetchone()
        return _parse_json_fields(dict(row)) if row else None
    finally:
        conn.close()


def session_stats(session_id: str) -> Dict[str, Any]:
    items = get_inspections(session_id, limit=200)
    counts = {"compliant": 0, "non_compliant": 0, "needs_review": 0}
    for item in items:
        status = item.get("compliance_status") or item.get("status") or "needs_review"
        counts[status] = counts.get(status, 0) + 1
    resolved = counts.get("compliant", 0) + counts.get("non_compliant", 0)
    return {
        "total": len(items),
        **counts,
        "compliance_rate": round(100 * counts.get("compliant", 0) / resolved) if resolved else None,
    }


def purge_expired_guest_data(now_epoch: Optional[int] = None) -> List[str]:
    """Delete expired guest inspection rows and return affected session IDs.

    Evidence files are deleted by the API startup layer so this module stays free of
    filesystem side effects. Reviewer/demo-reviewer rows are intentionally retained.
    """
    init_db()
    now_epoch = int(now_epoch or datetime.now(timezone.utc).timestamp())
    conn, db_type = _get_connection()
    try:
        cur = conn.cursor()
        ph = "%s" if db_type == "postgres" else "?"
        cur.execute(
            f"SELECT DISTINCT session_id FROM inspections WHERE role='guest' AND session_expires_at IS NOT NULL "
            f"AND session_expires_at <= {ph}",
            (now_epoch,),
        )
        session_ids = [r[0] for r in cur.fetchall() if r and r[0]]
        cur.execute(
            f"DELETE FROM inspections WHERE role='guest' AND session_expires_at IS NOT NULL AND session_expires_at <= {ph}",
            (now_epoch,),
        )
        if session_ids:
            placeholders = ",".join([ph] * len(session_ids))
            cur.execute(
                f"DELETE FROM products_catalog WHERE provenance='user_confirmed' AND confirmed_by_session IN ({placeholders})",
                tuple(session_ids),
            )
        conn.commit()
        return session_ids
    finally:
        conn.close()


def delete_session_data(session_id: str) -> int:
    """Delete one workspace's inspections and session-scoped catalogue confirmations."""
    init_db()
    conn, db_type = _get_connection()
    try:
        cur = conn.cursor(); ph = "%s" if db_type == "postgres" else "?"
        cur.execute(f"DELETE FROM inspections WHERE session_id={ph}", (session_id,))
        deleted = int(cur.rowcount or 0)
        cur.execute(
            f"DELETE FROM products_catalog WHERE provenance='user_confirmed' AND confirmed_by_session={ph}",
            (session_id,),
        )
        conn.commit()
        return deleted
    finally:
        conn.close()


def verify_session_chain(session_id: str) -> Dict[str, Any]:
    """Recompute the stored session hash chain and report the first break, if any."""
    init_db()
    conn, db_type = _get_connection()
    try:
        cur = _dict_cursor(conn, db_type); ph = "%s" if db_type == "postgres" else "?"
        cur.execute(
            f"SELECT id,previous_hash,record_hash,audit_payload FROM inspections WHERE session_id={ph} ORDER BY id ASC",
            (session_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    previous = None
    checked = 0
    for row in rows:
        payload = row.get("audit_payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return {"valid": False, "checked": checked, "broken_at": row.get("id"), "reason": "Stored audit payload is unreadable"}
        if not isinstance(payload, dict):
            return {"valid": False, "checked": checked, "broken_at": row.get("id"), "reason": "Stored audit payload is missing"}
        if (row.get("previous_hash") or None) != previous:
            return {"valid": False, "checked": checked, "broken_at": row.get("id"), "reason": "Previous-hash link mismatch"}
        expected = chained_record_hash(previous, payload)
        if expected != row.get("record_hash"):
            return {"valid": False, "checked": checked, "broken_at": row.get("id"), "reason": "Record hash mismatch"}
        previous = row.get("record_hash")
        checked += 1
    return {"valid": True, "checked": checked, "head": previous}


def delete_session_data(session_id: str) -> int:
    """Delete all inspection rows for one signed workspace."""
    init_db()
    conn, db_type = _get_connection()
    try:
        cur = conn.cursor()
        ph = "%s" if db_type == "postgres" else "?"
        cur.execute(f"DELETE FROM inspections WHERE session_id={ph}", (session_id,))
        deleted = int(cur.rowcount or 0)
        conn.commit()
        return deleted
    finally:
        conn.close()


def verify_session_chain(session_id: str) -> Dict[str, Any]:
    """Recompute the stored per-session hash chain from canonical audit payloads."""
    init_db()
    conn, db_type = _get_connection()
    try:
        cur = _dict_cursor(conn, db_type)
        ph = "%s" if db_type == "postgres" else "?"
        cur.execute(
            f"SELECT id,previous_hash,record_hash,audit_payload FROM inspections WHERE session_id={ph} ORDER BY id ASC",
            (session_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        previous = None
        for row in rows:
            stored_prev = row.get("previous_hash") or None
            if stored_prev != previous:
                return {"valid": False, "records": len(rows), "failed_at": row.get("id"), "reason": "previous-hash link mismatch"}
            raw = row.get("audit_payload")
            try:
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                payload = None
            if not isinstance(payload, dict):
                return {"valid": False, "records": len(rows), "failed_at": row.get("id"), "reason": "audit payload missing or invalid"}
            calculated = chained_record_hash(previous, payload)
            if calculated != row.get("record_hash"):
                return {"valid": False, "records": len(rows), "failed_at": row.get("id"), "reason": "record hash mismatch"}
            previous = row.get("record_hash")
        return {"valid": True, "records": len(rows), "head": previous}
    finally:
        conn.close()


def register_or_update_product(
    product_name: str,
    session_id: str,
    company: Optional[str] = None,
    quantity: Optional[str] = None,
    fssai: Optional[str] = None,
    mrp: Optional[float] = None,
    provenance: str = "user_confirmed",
) -> int:
    """Teach only explicitly confirmed/imported catalogue entries."""
    if provenance not in {"user_confirmed", "reviewer_confirmed", "catalog_import"}:
        raise ValueError("Unverified OCR guesses cannot be registered in the product catalog")
    if not product_name or len(product_name.strip()) < 2:
        raise ValueError("Product name required")
    init_db()
    conn, db_type = _get_connection()
    try:
        cur = conn.cursor()
        ph = "%s" if db_type == "postgres" else "?"
        cur.execute(f"SELECT id FROM products_catalog WHERE LOWER(product_name)=LOWER({ph}) LIMIT 1", (product_name.strip(),))
        row = cur.fetchone()
        existing = row[0] if row else None
        verified = True if db_type == "postgres" else 1
        if existing:
            sql = (
                f"UPDATE products_catalog SET company=COALESCE({ph},company), quantity=COALESCE({ph},quantity), "
                f"fssai=COALESCE({ph},fssai), mrp=COALESCE({ph},mrp), times_inspected=times_inspected+1, "
                f"last_inspected=CURRENT_TIMESTAMP, verified={ph}, provenance={ph}, confirmed_by_session={ph} WHERE id={ph}"
            )
            cur.execute(sql, (company, quantity, fssai, mrp, verified, provenance, session_id, existing))
            new_id = int(existing)
        else:
            fields = "product_name,company,quantity,fssai,mrp,times_inspected,verified,provenance,confirmed_by_session"
            vals = (product_name.strip(), company, quantity, fssai, mrp, 1, verified, provenance, session_id)
            placeholders = ",".join([ph] * len(vals))
            if db_type == "postgres":
                cur.execute(f"INSERT INTO products_catalog ({fields}) VALUES ({placeholders}) RETURNING id", vals)
                new_id = int(cur.fetchone()[0])
            else:
                cur.execute(f"INSERT INTO products_catalog ({fields}) VALUES ({placeholders})", vals)
                new_id = int(cur.lastrowid)
        conn.commit()
        return new_id
    finally:
        conn.close()


def lookup_product(
    fssai: Optional[str] = None,
    company: Optional[str] = None,
    quantity: Optional[str] = None,
    raw_text: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Optional[str]:
    """Look up trusted catalogue entries without cross-guest poisoning.

    Reviewer-confirmed/catalog-imported entries are global. A guest-confirmed title
    is reusable only inside the same signed workspace.
    """
    init_db()
    conn, db_type = _get_connection()
    try:
        cur = conn.cursor()
        ph = "%s" if db_type == "postgres" else "?"
        verified_clause = "verified = TRUE" if db_type == "postgres" else "verified = 1"
        trust_clause = f"{verified_clause} AND (provenance IN ('reviewer_confirmed','catalog_import') OR confirmed_by_session={ph})"
        sid = session_id or ""
        if fssai:
            cur.execute(
                f"SELECT product_name FROM products_catalog WHERE {trust_clause} AND fssai={ph} "
                "ORDER BY times_inspected DESC LIMIT 1",
                (sid, str(fssai)),
            )
            row = cur.fetchone()
            if row:
                return row[0]
        if company and quantity:
            op = "ILIKE" if db_type == "postgres" else "LIKE"
            cur.execute(
                f"SELECT product_name FROM products_catalog WHERE {trust_clause} AND company {op} {ph} "
                f"AND LOWER(quantity)=LOWER({ph}) ORDER BY times_inspected DESC LIMIT 1",
                (sid, f"%{company[:24]}%", quantity),
            )
            row = cur.fetchone()
            if row:
                return row[0]
        if raw_text:
            cur.execute(f"SELECT product_name FROM products_catalog WHERE {trust_clause} ORDER BY times_inspected DESC LIMIT 100", (sid,))
            for row in cur.fetchall():
                if row[0] and row[0].lower() in raw_text.lower():
                    return row[0]
        return None
    finally:
        conn.close()


def get_catalog(limit: int = 50) -> List[Dict[str, Any]]:
    init_db()
    conn, db_type = _get_connection()
    try:
        cur = _dict_cursor(conn, db_type)
        ph = "%s" if db_type == "postgres" else "?"
        verified_clause = "verified = TRUE" if db_type == "postgres" else "verified = 1"
        cur.execute(
            f"SELECT * FROM products_catalog WHERE {verified_clause} ORDER BY times_inspected DESC,id DESC LIMIT {ph}",
            (min(max(int(limit), 1), 200),),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


try:
    init_db()
except Exception as exc:
    print(f"[DB INIT] {exc}")
