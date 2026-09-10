"""Smoke tests for role access, cases, admin analytics and audit."""
import os, tempfile
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ["SQLITE_PATH"] = str(Path(_tmp.name)/"test.db")
os.environ["EVIDENCE_DIR"] = str(Path(_tmp.name)/"evidence")
os.environ["LABLELENS_SECRET"] = "test-secret-that-is-long-enough-for-smoke-tests"
os.environ["ALLOW_DEMO_OFFICER"] = "true"
os.environ["DEMO_OFFICER_CODE"] = "officer-test-code"
os.environ["DEMO_OFFICER_ID"] = "officer-test"
os.environ["DEMO_OFFICER_LABEL"] = "Test Officer"
os.environ["ALLOW_DEMO_ADMIN"] = "true"
os.environ["DEMO_ADMIN_CODE"] = "admin-test-code"
os.environ["DEMO_ADMIN_ID"] = "admin-test"
os.environ["DEMO_ADMIN_LABEL"] = "Test Admin"

from fastapi.testclient import TestClient
from main import app


def auth(token): return {"Authorization": "Bearer "+token}

with TestClient(app) as c:
    guest=c.post('/api/session/guest').json(); assert guest['role']=='guest'
    assert c.get('/api/admin/overview',headers=auth(guest['token'])).status_code==403

    officer_resp=c.post('/api/session/staff',json={'role':'officer','code':'officer-test-code'}); assert officer_resp.status_code==200, officer_resp.text
    officer=officer_resp.json(); assert officer['role']=='officer'
    assert c.get('/api/admin/overview',headers=auth(officer['token'])).status_code==403

    listing='''Generic Name: Liquid Detergent\nManufactured by: Demo Consumer Products Pvt Ltd\nPlot 21 Industrial Estate Pune Maharashtra 411001\nNet Qty: 500 ml\nConsumer Care: Demo Consumer Products Pvt Ltd\nPhone: +91 9876543210\nEmail: care@example.com'''
    r=c.post('/api/analyze/text',headers=auth(officer['token']),json={'text':listing,'product_name':'Missing MRP Test','evidence_scope':'complete_listing','category':'general'})
    assert r.status_code==200, r.text
    inspection=r.json(); assert inspection['compliance_status']=='non_compliant', inspection['compliance_status']

    cr=c.post(f"/api/inspections/{inspection['inspection_id']}/complaints",headers=auth(officer['token']),json={'remarks':'MRP declaration appears absent in complete listing evidence','priority':'high','submit':True,'case_type':'compliance_case'})
    assert cr.status_code==200, cr.text
    case=cr.json(); assert case['status']=='submitted'; assert case['violations']

    admin_resp=c.post('/api/session/staff',json={'role':'admin','code':'admin-test-code'}); assert admin_resp.status_code==200, admin_resp.text
    admin=admin_resp.json(); assert admin['role']=='admin'
    overview=c.get('/api/admin/overview',headers=auth(admin['token'])); assert overview.status_code==200; assert overview.json()['total_inspections']>=1
    cases=c.get('/api/admin/complaints',headers=auth(admin['token'])); assert cases.status_code==200 and len(cases.json())>=1
    officers=c.get('/api/admin/officers',headers=auth(admin['token'])); assert officers.status_code==200 and any(x['actor_id']=='officer-test' for x in officers.json())
    analytics=c.get('/api/admin/analytics?days=7',headers=auth(admin['token'])); assert analytics.status_code==200 and 'ocr_performance' in analytics.json()
    upd=c.patch(f"/api/admin/complaints/{case['id']}",headers={**auth(admin['token']),'Content-Type':'application/json'},json={'status':'under_review','admin_notes':'Evidence queued for human review','priority':'high'})
    assert upd.status_code==200, upd.text; assert upd.json()['status']=='under_review'
    audit=c.get('/api/admin/audit',headers=auth(admin['token'])); assert audit.status_code==200 and any(x['action']=='complaint_status_changed' for x in audit.json())
    health=c.get('/api/admin/system-health',headers=auth(admin['token'])); assert health.status_code==200 and health.json()['database']=='ok'
    page=c.get('/admin'); assert page.status_code==200 and 'Admin Console' in page.text

print('PASS: management/admin smoke tests')
