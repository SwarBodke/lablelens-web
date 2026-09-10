# LableLens — Legal Metrology Compliance Screening

LableLens is an AI-assisted packaged-commodity compliance screening platform. It combines physical-package and e-commerce evidence extraction with a versioned Legal Metrology ruleset, while clearly separating confirmed findings from evidence that still requires human review.

> **Important:** LableLens is a decision-support system. It does not issue enforcement orders, establish government authority, replace the current Gazette text, or automatically resolve every commodity-specific exemption or requirement.

## What this build fixes

- Three-state outcome: **Compliant / Potential non-compliance / Needs review**.
- Six core grouped checks now participate consistently in the overall result: responsible entity & commodity identity, net quantity, MRP + unit sale price, date/shelf-life context, consumer care, and calibrated font size.
- Responsible entity screening checks both identity and a locatable address; imported-product context can trigger country-of-origin screening.
- Rule 7 font-size screening only produces a physical pass/fail when a calibrated character height and PDP area are supplied.
- Multi-panel package capture (up to four images) is exposed in the UI so absence from one photograph is not treated as absence from the whole package.
- E-commerce listing-text screening is a first-class source type; month/year is treated separately under the e-commerce display context.
- OCR is hybrid: current `rapidocr` + ONNX Runtime when available, with Tesseract fallback/ensemble passes. Tesseract executable lookup is portable rather than hard-coded to Windows.
- English + Hindi Tesseract language configuration is supported through `LABLELENS_OCR_LANGS` when the corresponding language packs are installed.
- OCR confidence and extraction completeness are separate metrics. Direct pasted listing text is correctly labelled **N/A / direct text**, not fake OCR confidence.
- Barcode/QR identifiers are decoded when OpenCV can read them; decoding an identifier is not treated as proof of compliance.
- Guest workspaces are cryptographically signed, expire after 24 hours, have a configurable scan limit, and history is scoped to that workspace.
- No fake Google login or fake government-officer verification. Optional reviewer mode is explicitly a demo role and is disabled by default.
- PDF, DOCX and JSON reports are real downloads.
- Uploaded image bytes receive SHA-256 hashes. Stored inspections are also linked in a per-session SHA-256 record chain. This is tamper-evident **within the stored application history**, not an externally notarised chain of custody.
- Product catalogue learning happens only after explicit confirmation/import; raw OCR guesses never automatically become catalogue truth.
- Offline behaviour is truthful: the app shell and text/context draft can work locally; OCR/history/reporting still require the backend.
- No product-specific database rewrite hacks.
- Month/year expiry checks use the end of the declared month rather than the first day.
- Suspicious OCR values are surfaced for review rather than silently “corrected”.

## Recommended: Docker

```bash
cp .env.example .env
# Set a strong persistent LABLELENS_SECRET in .env

docker build -t lablelens .
docker run --rm -p 8000:8000 --env-file .env lablelens
```

Open `http://localhost:8000`.

The Docker image installs Tesseract English and Hindi language packs and Python dependencies from `requirements.txt`.

## Local Python setup

Use Python 3.11+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Install the **Tesseract executable** separately on the OS. The Python package `pytesseract` is only the wrapper.

Set environment variables (examples are in `.env.example`), then run:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

If Tesseract is not on `PATH`, set `TESSERACT_CMD` to its executable. For Hindi recognition, install the `hin` language data and use:

```text
LABLELENS_OCR_LANGS=eng+hin
```

## Important deployment variables

- `LABLELENS_SECRET` — **required for a persistent deployment**. If omitted, a random secret is generated at process start and existing session tokens become invalid after a restart.
- `GUEST_SCAN_LIMIT` — default `20` inspections per guest workspace.
- `PRESERVE_EVIDENCE` — default `true`.
- `EVIDENCE_DIR` — where original uploaded evidence is stored.
- `SQLITE_PATH` — SQLite file path when PostgreSQL is not configured.
- `DATABASE_URL` / `POSTGRES_URL` — optional PostgreSQL connection URL.
- `ALLOWED_ORIGINS` — comma-separated CORS origins if the frontend/API are deployed on different origins.
- `ALLOW_DEMO_REVIEWER=true` plus `DEMO_REVIEWER_CODE=...` — optional **project-only** demo reviewer role.

For a real production deployment, use durable object storage for evidence, PostgreSQL, TLS, proper organisation/user identity, secrets management, access logs and retention policy—not the locally configured reviewer mechanism.

## Evidence scope matters

For physical packages, leave **“I captured all declaration-bearing panels”** unchecked unless the package has actually been covered. Under partial evidence, a missing field becomes **Needs review**, not an accusation that the package is non-compliant.

For font size, provide measured character height and Principal Display Panel area if you want the Rule 7 baseline to resolve. An arbitrary phone photo cannot reliably convert pixels to statutory millimetres without calibration.

## Ruleset

The current versioned baseline is exposed at:

```text
GET /api/ruleset
```

The ruleset source metadata points to Department of Consumer Affairs material and records its review date. The six grouped checks are an automation-oriented baseline, not an exhaustive consolidation of every LMPC rule, amendment, proviso or sector-specific requirement.

## API highlights

```text
POST /api/session/guest
POST /api/analyze              # one physical image
POST /api/analyze/multi        # 1–4 physical package panels
POST /api/analyze/text         # e-commerce listing text
GET  /api/inspections
GET  /api/inspections/{id}
GET  /api/inspections/{id}/report.pdf
GET  /api/inspections/{id}/report.docx
GET  /api/inspections/{id}/report.json
POST /api/catalog/confirm
GET  /health
GET  /api/ruleset
```

Except for `/`, `/health`, `/api/ruleset`, and session creation, API routes use the signed bearer workspace token.

## Validation

Run the included smoke tests:

```bash
python smoke_test.py
```

The test covers key rule-engine regressions, session isolation, guest limit handling, stored record hashes, and PDF/DOCX/JSON report generation.

## Cloudflare Quick Tunnel note

`start_live.py`/`cloudflared.exe` can be useful for a temporary preview tunnel, but a `trycloudflare.com` Quick Tunnel is not a stable production deployment. For persistent use, deploy the same code behind a stable hostname/service and set a persistent `LABLELENS_SECRET`.

## Adaptive OCR routing (v2.4)

The production default `LABLELENS_OCR_MODE=adaptive` is now **Tesseract-first with an accuracy guard**. A single full-label Tesseract pass handles clean, flat labels. LableLens only accepts that fast path when reader confidence, readable-text density, declaration coverage, and core evidence anchors all clear strict thresholds.

If the first pass is incomplete, LableLens runs only the relevant cheap Tesseract top/bottom recovery zones. If the strict gate still does not pass, the image automatically escalates to RapidOCR PP-OCRv6 through ONNX Runtime. OCR evidence is merged additively, so RapidOCR cannot erase a good Tesseract read. A raw PSM 3 Tesseract pass remains available as a last-resort accuracy guard when the combined evidence is still materially incomplete.

For regression testing, `ensemble` forces RapidOCR plus all four Tesseract passes. `rapid_first` preserves the prior v2.3 routing for A/B comparisons; `rapid_only` and `tesseract_only` are diagnostic modes. The API returns per-panel routes, timing, escalation reasons, confidence, and coverage so the fast path can be audited rather than assumed.

### Measure the optimization on your machine

Run `python benchmark_ocr.py /path/to/product.jpg`. The script compares default adaptive routing with the forced full ensemble on the same image, prints wall time and extracted fields, and performs an accuracy-parity check. If the adaptive route misses or changes a key value that the ensemble recovered, the benchmark reports the mismatch explicitly.


## Admin & officer management (v2.5)

LableLens now includes a database-backed officer/case management layer without changing the Smart OCR execution path. The administrator console is served at `/admin`; its APIs enforce the `admin` role server-side. Officers can create compliance cases only from stored `non_compliant` findings, while `needs_review` findings use a separate manual-review case type so OCR uncertainty is not converted into a legal allegation.

Configure deployment-only staff access with environment variables before starting Uvicorn:

```bash
export LABLELENS_SECRET='replace-with-a-long-random-secret'
export ALLOW_DEMO_OFFICER=true
export DEMO_OFFICER_CODE='replace-with-a-strong-officer-code'
export DEMO_OFFICER_ID='officer-001'
export DEMO_OFFICER_LABEL='Officer 001'
export ALLOW_DEMO_ADMIN=true
export DEMO_ADMIN_CODE='replace-with-a-strong-admin-code'
export DEMO_ADMIN_ID='admin-001'
export DEMO_ADMIN_LABEL='Administrator'
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

These codes are a prototype deployment access mechanism, **not identity verification**. The role model and protected APIs are intentionally separated from the login mechanism so OAuth/SSO can replace the codes later. Do not deploy with weak/shared codes.

### Management APIs

- `POST /api/session/staff` — deployment-configured officer/admin session
- `POST /api/inspections/{id}/complaints` — create a compliance case or manual-review request
- `GET/PATCH /api/cases/...` — officer-owned case workflow
- `GET /api/admin/overview` — real KPI summary
- `GET /api/admin/analytics?days=7|30|90` — compliance, violation, officer, OCR-route and latency series
- `GET /api/admin/officers` and `/api/admin/officers/{id}` — officer monitoring
- `GET /api/admin/inspections` — cross-officer inspection repository
- `GET/PATCH /api/admin/complaints/...` — admin case review/status transitions
- `GET /api/admin/audit` — append-only action log
- `GET /api/admin/system-health` — non-sensitive platform/OCR/ruleset health

Dashboard statistics are derived from stored records; the application does not auto-seed fake numbers.


## Google staff sign-in

Admin and Officer workspaces use Google Identity Services. Google authenticates the account; LableLens assigns the role only from backend allowlists. A user cannot choose or elevate their own role in the browser.

Set at minimum:

```bash
export LABLELENS_SECRET='replace-with-a-long-random-secret'
export GOOGLE_CLIENT_ID='YOUR_WEB_CLIENT_ID.apps.googleusercontent.com'
export GOOGLE_ADMIN_EMAILS='admin@gmail.com'
export GOOGLE_OFFICER_EMAILS='officer1@gmail.com,officer2@gmail.com'
```

Optional Google Workspace domain mapping is available with `GOOGLE_ADMIN_DOMAINS` and `GOOGLE_OFFICER_DOMAINS`. Exact-email allowlists are safer for small deployments.

In Google Cloud / Google Auth Platform create a **Web application** client and add every browser origin that will host LableLens, for example `http://localhost:8000` for local development and your stable HTTPS production origin. Random `trycloudflare.com` Quick Tunnel hostnames change, so a newly generated tunnel hostname must be added as an authorized JavaScript origin before Google Sign-In will work there. A stable named Cloudflare Tunnel/custom domain is recommended for staff sign-in.

The old demo-code API remains available only for backward-compatible testing and is disabled unless its explicit `ALLOW_DEMO_*` environment flags are enabled. It is no longer exposed in the normal login UI.
