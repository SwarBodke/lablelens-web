"""Admin/officer management, compliance cases, analytics and audit utilities.

This layer is deliberately separate from OCR/compliance execution so dashboard queries
never run on the inspection hot path except for lightweight audit writes.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from database import _get_connection, _dict_cursor, _parse_json_fields, init_db

CASE_STATUSES = {"draft", "submitted", "under_review", "action_required", "resolved", "closed", "rejected"}
CASE_PRIORITIES = {"low", "normal", "high", "urgent"}
ALLOWED_TRANSITIONS = {
    "draft": {"submitted", "closed"},
    "submitted": {"under_review", "action_required", "resolved", "rejected", "closed"},
    "under_review": {"action_required", "resolved", "rejected", "closed"},
    "action_required": {"under_review", "resolved", "rejected", "closed"},
    "resolved": {"closed", "under_review"},
    "rejected": {"closed", "under_review"},
    "closed": {"under_review"},
}


def _ph(db_type: str) -> str:
    return "%s" if db_type == "postgres" else "?"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rowdict(row: Any) -> Dict[str, Any]:
    return dict(row) if row is not None else {}


def register_actor(session: Dict[str, Any]) -> None:
    if session.get("role") not in {"admin", "officer"}:
        return
    init_db()
    conn, db_type = _get_connection()
    try:
        cur = conn.cursor(); ph = _ph(db_type)
        actor_id = session.get("actor_id") or session.get("sid")
        now = _now_iso()
        if db_type == "postgres":
            cur.execute(
                """INSERT INTO actors(actor_id,display_name,role,status,last_login)
                   VALUES(%s,%s,%s,'active',%s)
                   ON CONFLICT(actor_id) DO UPDATE SET display_name=EXCLUDED.display_name, role=EXCLUDED.role,
                   status='active', last_login=EXCLUDED.last_login""",
                (actor_id, session.get("label") or session.get("role"), session.get("role"), now),
            )
        else:
            cur.execute(
                """INSERT INTO actors(actor_id,display_name,role,status,last_login)
                   VALUES(?,?,?,'active',?)
                   ON CONFLICT(actor_id) DO UPDATE SET display_name=excluded.display_name, role=excluded.role,
                   status='active', last_login=excluded.last_login""",
                (actor_id, session.get("label") or session.get("role"), session.get("role"), now),
            )
        conn.commit()
    finally:
        conn.close()


def audit_event(session: Optional[Dict[str, Any]], action: str, entity_type: str = "system", entity_id: Optional[Any] = None, details: Optional[Dict[str, Any]] = None) -> None:
    init_db()
    conn, db_type = _get_connection()
    try:
        cur = conn.cursor(); ph = _ph(db_type)
        sess = session or {}
        cur.execute(
            f"INSERT INTO audit_events(actor_id,actor_label,actor_role,action,entity_type,entity_id,details) VALUES({','.join([ph]*7)})",
            (
                sess.get("actor_id") or sess.get("sid"), sess.get("label"), sess.get("role"), action,
                entity_type, str(entity_id) if entity_id is not None else None,
                json.dumps(details or {}, ensure_ascii=False, default=str),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_inspection_any(inspection_id: int) -> Optional[Dict[str, Any]]:
    init_db(); conn, db_type = _get_connection()
    try:
        cur = _dict_cursor(conn, db_type); ph = _ph(db_type)
        cur.execute(f"SELECT * FROM inspections WHERE id={ph}", (inspection_id,))
        row = cur.fetchone()
        return _parse_json_fields(_rowdict(row)) if row else None
    finally:
        conn.close()


def get_inspection_for_actor(inspection_id: int, session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    item = get_inspection_any(inspection_id)
    if not item:
        return None
    if session.get("role") == "admin":
        return item
    if session.get("role") == "officer" and item.get("actor_id") == session.get("actor_id"):
        return item
    if item.get("session_id") == session.get("sid"):
        return item
    return None


def _check_rule_map(checks: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    iterable = checks.values() if isinstance(checks, dict) else (checks or [])
    for c in iterable:
        if not isinstance(c, dict):
            continue
        title = str(c.get("title") or c.get("id") or "").lower()
        rule = str(c.get("rule") or "")
        for key in ["mrp", "quantity", "manufacturer", "address", "date", "consumer", "font", "origin", "unit sale", "unit"]:
            if key in title:
                out[key] = rule
    return out


def _violation_type(text: str) -> str:
    t = (text or "").lower()
    mapping = [
        ("mrp", "mrp"), ("maximum retail", "mrp"), ("net quantity", "net_quantity"), ("quantity", "net_quantity"),
        ("manufacturer", "manufacturer_address"), ("address", "manufacturer_address"), ("consumer", "consumer_care"),
        ("month", "date"), ("date", "date"), ("country", "country_of_origin"), ("origin", "country_of_origin"),
        ("unit sale", "unit_sale_price"), ("font", "display_font"), ("display", "display_font"),
    ]
    for needle, category in mapping:
        if needle in t:
            return category
    return "other"


def _rule_for_violation(text: str, inspection: Dict[str, Any]) -> str:
    rules = _check_rule_map(inspection.get("checks"))
    vt = _violation_type(text)
    keys = {
        "mrp": ["mrp"], "net_quantity": ["quantity", "unit"], "manufacturer_address": ["manufacturer", "address"],
        "date": ["date"], "consumer_care": ["consumer"], "country_of_origin": ["origin"],
        "unit_sale_price": ["unit sale"], "display_font": ["font"],
    }.get(vt, [])
    for key in keys:
        if rules.get(key):
            return rules[key]
    return "See stored inspection check"


def create_case(inspection_id: int, session: Dict[str, Any], *, remarks: str = "", priority: str = "normal", submit: bool = False, case_type: str = "compliance_case") -> Dict[str, Any]:
    if session.get("role") not in {"officer", "admin"}:
        raise PermissionError("Officer or admin access required")
    inspection = get_inspection_for_actor(inspection_id, session)
    if not inspection:
        raise LookupError("Inspection not found or not accessible")
    status = inspection.get("compliance_status") or inspection.get("status") or "needs_review"
    if case_type == "compliance_case" and status != "non_compliant":
        raise ValueError("A compliance case can only be created from a potential non-compliance result")
    if case_type == "manual_review" and status != "needs_review":
        raise ValueError("Manual review requests are only used for Needs Review inspections")
    finding = inspection.get("lmpc") or inspection.get("pcr_2011") or {}
    violations = finding.get("violations") or inspection.get("violations") or []
    if isinstance(violations, str):
        try: violations = json.loads(violations)
        except Exception: violations = [violations]
    if case_type == "compliance_case" and not violations:
        raise ValueError("No positively detected violation is stored for this inspection")
    if priority not in CASE_PRIORITIES: priority = "normal"
    initial_status = "submitted" if submit else "draft"
    init_db(); conn, db_type = _get_connection()
    try:
        cur = conn.cursor(); ph = _ph(db_type)
        vals = (inspection_id, session.get("actor_id") or session.get("sid"), session.get("label"), case_type, priority, initial_status, remarks[:4000])
        if db_type == "postgres":
            cur.execute(f"INSERT INTO complaints(inspection_id,created_by_actor,created_by_label,case_type,priority,status,officer_remarks) VALUES({','.join([ph]*7)}) RETURNING id", vals)
            case_id = int(cur.fetchone()[0])
        else:
            cur.execute(f"INSERT INTO complaints(inspection_id,created_by_actor,created_by_label,case_type,priority,status,officer_remarks) VALUES({','.join([ph]*7)})", vals)
            case_id = int(cur.lastrowid)
        evidence_text = (inspection.get("raw_text") or "")[:12000]
        items = violations if case_type == "compliance_case" else (finding.get("needs_review") or inspection.get("needs_review") or ["Manual review requested"])
        for item in items:
            desc = str(item)
            cur.execute(
                f"INSERT INTO complaint_violations(complaint_id,violation_type,description,rule_reference,evidence_text) VALUES({','.join([ph]*5)})",
                (case_id, _violation_type(desc), desc[:2000], _rule_for_violation(desc, inspection), evidence_text),
            )
        cur.execute(
            f"INSERT INTO complaint_status_history(complaint_id,old_status,new_status,actor_id,actor_label,notes) VALUES({','.join([ph]*6)})",
            (case_id, None, initial_status, session.get("actor_id") or session.get("sid"), session.get("label"), "Case created"),
        )
        conn.commit()
    finally:
        conn.close()
    audit_event(session, "complaint_submitted" if submit else "complaint_drafted", "complaint", case_id, {"inspection_id": inspection_id, "case_type": case_type})
    return get_case(case_id, session, admin_override=session.get("role") == "admin") or {"id": case_id}


def get_case(case_id: int, session: Dict[str, Any], admin_override: bool = False) -> Optional[Dict[str, Any]]:
    init_db(); conn, db_type = _get_connection()
    try:
        cur = _dict_cursor(conn, db_type); ph = _ph(db_type)
        sql = f"SELECT c.*, i.product_name, i.company, i.manufacturer, i.created_at AS inspection_created_at, i.compliance_status, i.ocr_confidence, i.evidence, i.raw_text, i.checks, i.ruleset_id, i.engine_version, i.ocr_route, i.processing_time_ms, i.actor_id AS inspection_actor_id, i.user_label AS officer_label FROM complaints c JOIN inspections i ON i.id=c.inspection_id WHERE c.id={ph}"
        params: List[Any] = [case_id]
        if not admin_override and session.get("role") != "admin":
            sql += f" AND c.created_by_actor={ph}"; params.append(session.get("actor_id") or session.get("sid"))
        cur.execute(sql, tuple(params)); row = cur.fetchone()
        if not row: return None
        item = _rowdict(row)
        for field in ["manufacturer", "evidence", "checks"]:
            if isinstance(item.get(field), str):
                try: item[field] = json.loads(item[field])
                except Exception: pass
        cur.execute(f"SELECT * FROM complaint_violations WHERE complaint_id={ph} ORDER BY id", (case_id,))
        if isinstance(item.get("evidence"), list):
            item["evidence"] = [{k:v for k,v in ev.items() if k != "path"} for ev in item["evidence"]]
        item["violations"] = [_rowdict(r) for r in cur.fetchall()]
        cur.execute(f"SELECT * FROM complaint_status_history WHERE complaint_id={ph} ORDER BY id", (case_id,))
        item["status_history"] = [_rowdict(r) for r in cur.fetchall()]
        return item
    finally:
        conn.close()


def list_cases(session: Dict[str, Any], *, limit: int = 100, status: Optional[str] = None, officer: Optional[str] = None, violation_type: Optional[str] = None, priority: Optional[str] = None, product: Optional[str] = None, manufacturer: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None) -> List[Dict[str, Any]]:
    init_db(); conn, db_type = _get_connection()
    try:
        cur = _dict_cursor(conn, db_type); ph = _ph(db_type)
        sql = """SELECT DISTINCT c.*, i.product_name, i.company, i.compliance_status, i.ocr_confidence,
                 i.created_at AS inspection_created_at, i.ocr_route, i.actor_id AS inspection_actor_id,
                 i.user_label AS officer_label FROM complaints c JOIN inspections i ON i.id=c.inspection_id"""
        params: List[Any] = []; where: List[str] = []
        if session.get("role") != "admin":
            where.append(f"c.created_by_actor={ph}"); params.append(session.get("actor_id") or session.get("sid"))
        if status:
            where.append(f"c.status={ph}"); params.append(status)
        if officer and session.get("role") == "admin":
            where.append(f"i.actor_id={ph}"); params.append(officer)
        if violation_type:
            sql += " JOIN complaint_violations cv ON cv.complaint_id=c.id"
            where.append(f"cv.violation_type={ph}"); params.append(violation_type)
        if priority:
            where.append(f"c.priority={ph}"); params.append(priority)
        if product:
            where.append(f"LOWER(i.product_name) LIKE {ph}"); params.append("%"+product.lower()+"%")
        if manufacturer:
            where.append(f"LOWER(COALESCE(i.company,'')) LIKE {ph}"); params.append("%"+manufacturer.lower()+"%")
        if date_from:
            where.append(f"c.created_at >= {ph}"); params.append(date_from)
        if date_to:
            where.append(f"c.created_at <= {ph}"); params.append(date_to + "T23:59:59+00:00" if len(date_to)==10 else date_to)
        if where: sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY c.id DESC LIMIT {ph}"; params.append(min(max(int(limit),1),500))
        cur.execute(sql, tuple(params)); return [_rowdict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def update_case(case_id: int, session: Dict[str, Any], *, new_status: str, admin_notes: str = "", priority: Optional[str] = None) -> Dict[str, Any]:
    if session.get("role") != "admin": raise PermissionError("Admin access required")
    if new_status not in CASE_STATUSES: raise ValueError("Invalid case status")
    current = get_case(case_id, session, admin_override=True)
    if not current: raise LookupError("Case not found")
    old = current.get("status") or "draft"
    if new_status != old and new_status not in ALLOWED_TRANSITIONS.get(old, set()):
        raise ValueError(f"Invalid status transition: {old} → {new_status}")
    init_db(); conn, db_type = _get_connection()
    try:
        cur = conn.cursor(); ph = _ph(db_type)
        fields = [f"status={ph}", f"admin_notes={ph}", "updated_at=CURRENT_TIMESTAMP"]; vals: List[Any] = [new_status, admin_notes[:4000]]
        if priority:
            if priority not in CASE_PRIORITIES: raise ValueError("Invalid priority")
            fields.append(f"priority={ph}"); vals.append(priority)
        vals.append(case_id)
        cur.execute(f"UPDATE complaints SET {', '.join(fields)} WHERE id={ph}", tuple(vals))
        if new_status != old:
            cur.execute(
                f"INSERT INTO complaint_status_history(complaint_id,old_status,new_status,actor_id,actor_label,notes) VALUES({','.join([ph]*6)})",
                (case_id, old, new_status, session.get("actor_id"), session.get("label"), admin_notes[:2000]),
            )
        conn.commit()
    finally:
        conn.close()
    audit_event(session, "complaint_status_changed" if new_status != old else "complaint_reviewed", "complaint", case_id, {"old_status": old, "new_status": new_status})
    return get_case(case_id, session, admin_override=True) or {}



def officer_update_case(case_id: int, session: Dict[str, Any], *, new_status: str, remarks: str = "") -> Dict[str, Any]:
    if session.get("role") not in {"officer", "admin"}:
        raise PermissionError("Officer or admin access required")
    current = get_case(case_id, session, admin_override=session.get("role") == "admin")
    if not current:
        raise LookupError("Case not found")
    old = current.get("status") or "draft"
    if session.get("role") == "officer" and not (old == "draft" and new_status in {"submitted", "closed"}):
        raise ValueError("Officers may only submit or close their own draft cases")
    if new_status not in CASE_STATUSES or (new_status != old and new_status not in ALLOWED_TRANSITIONS.get(old, set())):
        raise ValueError(f"Invalid status transition: {old} → {new_status}")
    init_db(); conn, db_type = _get_connection()
    try:
        cur = conn.cursor(); ph = _ph(db_type)
        cur.execute(f"UPDATE complaints SET status={ph}, officer_remarks={ph}, updated_at=CURRENT_TIMESTAMP WHERE id={ph}", (new_status, remarks[:4000] or current.get("officer_remarks") or "", case_id))
        if new_status != old:
            cur.execute(f"INSERT INTO complaint_status_history(complaint_id,old_status,new_status,actor_id,actor_label,notes) VALUES({','.join([ph]*6)})", (case_id, old, new_status, session.get("actor_id"), session.get("label"), remarks[:2000]))
        conn.commit()
    finally:
        conn.close()
    audit_event(session, "complaint_submitted" if new_status == "submitted" else "complaint_status_changed", "complaint", case_id, {"old_status": old, "new_status": new_status})
    return get_case(case_id, session, admin_override=session.get("role") == "admin") or {}


def list_all_inspections(limit: int = 200, *, actor_id: Optional[str] = None) -> List[Dict[str, Any]]:
    init_db(); conn, db_type = _get_connection()
    try:
        cur = _dict_cursor(conn, db_type); ph = _ph(db_type)
        if actor_id:
            cur.execute(f"SELECT * FROM inspections WHERE actor_id={ph} ORDER BY id DESC LIMIT {ph}", (actor_id, min(max(int(limit),1),1000)))
        else:
            cur.execute(f"SELECT * FROM inspections ORDER BY id DESC LIMIT {ph}", (min(max(int(limit),1),1000),))
        rows = [_parse_json_fields(_rowdict(r)) for r in cur.fetchall()]
        for item in rows:
            if isinstance(item.get("evidence"), list):
                item["evidence"] = [{k:v for k,v in ev.items() if k != "path"} for ev in item["evidence"]]
        return rows
    finally:
        conn.close()


def _dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value: return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def admin_overview() -> Dict[str, Any]:
    inspections = list_all_inspections(1000)
    init_db(); conn, db_type = _get_connection()
    try:
        cur = _dict_cursor(conn, db_type)
        cur.execute("SELECT * FROM complaints ORDER BY id DESC LIMIT 1000"); cases = [_rowdict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM actors WHERE role='officer' ORDER BY last_login DESC"); actors = [_rowdict(r) for r in cur.fetchall()]
    finally: conn.close()
    now = datetime.now(timezone.utc); today = now.date()
    counts = Counter((i.get("compliance_status") or i.get("status") or "needs_review") for i in inspections)
    resolved = counts["compliant"] + counts["non_compliant"]
    timings = [float(i["processing_time_ms"]) for i in inspections if i.get("processing_time_ms") is not None]
    active_cutoff = now - timedelta(hours=24)
    active = sum(1 for a in actors if (_dt(a.get("last_login")) or datetime.min.replace(tzinfo=timezone.utc)) >= active_cutoff)
    return {
        "total_inspections": len(inspections),
        "inspections_today": sum(1 for i in inspections if (_dt(i.get("created_at")) and _dt(i.get("created_at")).date() == today)),
        "compliant": counts["compliant"], "non_compliant": counts["non_compliant"], "needs_review": counts["needs_review"],
        "compliance_rate": round(100 * counts["compliant"] / resolved, 1) if resolved else None,
        "open_complaints": sum(1 for c in cases if c.get("status") not in {"resolved","closed","rejected"}),
        "active_officers": active,
        "average_ocr_time_ms": round(sum(timings)/len(timings),1) if timings else None,
        "recent_inspections": inspections[:8],
        "recent_complaints": cases[:8],
        "last_updated": _now_iso(),
    }


def analytics(days: int = 30) -> Dict[str, Any]:
    days = 7 if days <= 7 else 30 if days <= 30 else 90
    cutoff = datetime.now(timezone.utc) - timedelta(days=days-1)
    inspections = [i for i in list_all_inspections(5000) if (_dt(i.get("created_at")) or cutoff - timedelta(days=1)) >= cutoff]
    cases = []
    init_db(); conn, db_type = _get_connection()
    try:
        cur = _dict_cursor(conn, db_type); cur.execute("SELECT * FROM complaints ORDER BY id DESC LIMIT 5000"); cases = [_rowdict(r) for r in cur.fetchall()]
    finally: conn.close()
    trend: Dict[str, Counter] = defaultdict(Counter); latency: Dict[str, List[float]] = defaultdict(list)
    violations = Counter(); routes = defaultdict(lambda: {"count":0,"time":[],"confidence":[]}); officer_counts = Counter()
    for i in inspections:
        d = _dt(i.get("created_at")); key = d.date().isoformat() if d else "unknown"
        st = i.get("compliance_status") or "needs_review"; trend[key][st] += 1
        if i.get("processing_time_ms") is not None: latency[key].append(float(i["processing_time_ms"]))
        officer = i.get("user_label") or i.get("actor_id") or "Guest"; officer_counts[officer] += 1
        route = i.get("ocr_route") or "unknown"; routes[route]["count"] += 1
        if i.get("processing_time_ms") is not None: routes[route]["time"].append(float(i["processing_time_ms"]))
        if i.get("ocr_confidence") is not None: routes[route]["confidence"].append(float(i["ocr_confidence"]))
        raw_violations = i.get("violations") or []
        for v in raw_violations if isinstance(raw_violations,list) else []: violations[_violation_type(str(v))] += 1
    date_rows=[]; latency_rows=[]
    for offset in range(days):
        day=(cutoff+timedelta(days=offset)).date().isoformat(); c=trend.get(day,Counter())
        date_rows.append({"date":day,"compliant":c["compliant"],"non_compliant":c["non_compliant"],"needs_review":c["needs_review"],"total":sum(c.values())})
        vals=latency.get(day,[]); latency_rows.append({"date":day,"avg_ms":round(sum(vals)/len(vals),1) if vals else None})
    route_rows=[]
    for route,val in routes.items():
        route_rows.append({"route":route,"count":val["count"],"avg_ms":round(sum(val["time"])/len(val["time"]),1) if val["time"] else None,"avg_confidence":round(sum(val["confidence"])/len(val["confidence"]),1) if val["confidence"] else None})
    return {
        "days":days,"compliance_trend":date_rows,"latency_trend":latency_rows,
        "violation_distribution":[{"type":k,"count":v} for k,v in violations.most_common()],
        "officer_activity":[{"officer":k,"count":v} for k,v in officer_counts.most_common(20)],
        "compliance_breakdown":dict(Counter((i.get("compliance_status") or "needs_review") for i in inspections)),
        "complaint_status":dict(Counter(c.get("status") or "draft" for c in cases)),
        "ocr_performance":sorted(route_rows,key=lambda x:-x["count"]),
    }


def list_officers() -> List[Dict[str, Any]]:
    init_db(); conn, db_type = _get_connection()
    try:
        cur = _dict_cursor(conn, db_type); cur.execute("SELECT * FROM actors WHERE role='officer' ORDER BY last_login DESC"); actors=[_rowdict(r) for r in cur.fetchall()]
    finally: conn.close()
    inspections = list_all_inspections(5000); cases = []
    init_db(); conn, db_type = _get_connection()
    try:
        cur=_dict_cursor(conn,db_type); cur.execute("SELECT * FROM complaints ORDER BY id DESC LIMIT 5000"); cases=[_rowdict(r) for r in cur.fetchall()]
    finally: conn.close()
    out=[]; today=datetime.now(timezone.utc).date()
    for a in actors:
        aid=a.get("actor_id"); rows=[i for i in inspections if i.get("actor_id")==aid]; counts=Counter(i.get("compliance_status") or "needs_review" for i in rows)
        timings=[float(i["processing_time_ms"]) for i in rows if i.get("processing_time_ms") is not None]; conf=[float(i["ocr_confidence"]) for i in rows if i.get("ocr_confidence") is not None]
        activity_dates=[d for d in [_dt(a.get("last_login"))]+[_dt(i.get("created_at")) for i in rows] if d]
        last_activity=max(activity_dates).isoformat(timespec="seconds") if activity_dates else None
        out.append({**a,"total_inspections":len(rows),"inspections_today":sum(1 for i in rows if _dt(i.get("created_at")) and _dt(i.get("created_at")).date()==today),"compliant":counts["compliant"],"non_compliant":counts["non_compliant"],"needs_review":counts["needs_review"],"complaints_filed":sum(1 for c in cases if c.get("created_by_actor")==aid),"avg_ocr_confidence":round(sum(conf)/len(conf),1) if conf else None,"avg_processing_time_ms":round(sum(timings)/len(timings),1) if timings else None,"last_activity":last_activity})
    return out


def officer_detail(actor_id: str) -> Optional[Dict[str, Any]]:
    officers={x["actor_id"]:x for x in list_officers()}
    if actor_id not in officers: return None
    rows=list_all_inspections(500,actor_id=actor_id)
    return {"officer":officers[actor_id],"inspections":rows}


def list_audit(limit: int = 200) -> List[Dict[str, Any]]:
    init_db(); conn, db_type = _get_connection()
    try:
        cur=_dict_cursor(conn,db_type); ph=_ph(db_type); cur.execute(f"SELECT * FROM audit_events ORDER BY id DESC LIMIT {ph}",(min(max(int(limit),1),1000),)); rows=[]
        for r in cur.fetchall():
            d=_rowdict(r)
            if isinstance(d.get("details"),str):
                try:d["details"]=json.loads(d["details"])
                except Exception:pass
            rows.append(d)
        return rows
    finally: conn.close()
