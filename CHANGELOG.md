# 2.6.0 — Google staff identity

- Replaced visible Admin/Officer access-code login with Google Sign-In.
- Added server-side Google ID-token verification using `google-auth`.
- Added backend exact-email and optional Workspace-domain role allowlists.
- Staff roles are assigned only on the backend; the browser cannot select Admin vs Officer.
- Google `sub` is used as the stable actor identity while verified email/name are carried in the signed LableLens session.
- Guest mode remains available. Legacy demo-code endpoints remain disabled-by-default for compatibility/testing only.
- Smart OCR files and OCR routing logic were not changed.

# LableLens changelog

## 2.5.0 — Admin monitoring & compliance cases
- Added deployment-configured `admin` and `officer` roles with backend authorization.
- Added `/admin` operations console using real database KPIs and native SVG charts.
- Added officer monitoring, cross-officer inspection view, violation analytics, OCR-route/latency analytics and system health.
- Added compliance case/manual-review workflow linked to original inspections and evidence hashes.
- Added complaint status history, validated transitions and append-only audit events.
- Added officer-owned case list/draft submission and protected report/evidence access by actor role.
- Extended inspection persistence with stable actor ID, OCR route and processing time.
- Added additive database migrations/indexes; existing inspection records are preserved.
- Preserved Smart OCR: Tesseract fast path → targeted recovery → RapidOCR PP-OCRv6/ONNX escalation → final accuracy guard.

# 2.4.1 — Integrated Smart OCR

- Smart OCR is now the default website OCR path: Tesseract fast pass first, targeted Tesseract recovery second, RapidOCR PP-OCRv6/ONNX only when strict accuracy gates require escalation.
- RapidOCR recovery is additive and never overwrites stronger Tesseract evidence.
- Accuracy guard retains final raw Tesseract recovery for hard labels.
- Frontend health/status copy now reflects the real Tesseract-first routing strategy.
- `/api/analyze` and `/api/analyze/multi` both use the integrated Smart OCR engine automatically.

# LableLens v2.4.0 — Accuracy-Guarded Tesseract-First OCR

- Production `adaptive` mode now starts with one fast full-label Tesseract pass for clean/flat labels.
- Strict confidence, text-density, declaration-coverage, and core-anchor gates prevent unsafe fast-path acceptance.
- Missing evidence triggers targeted top/bottom Tesseract recovery before paying RapidOCR latency.
- RapidOCR PP-OCRv6/ONNX is an automatic escalation engine; recovered evidence is merged additively with Tesseract text.
- Raw PSM 3 remains a last-resort accuracy guard when combined evidence is still materially incomplete.
- Added `rapid_first` diagnostic mode to preserve the v2.3 router for A/B testing.
- Per-panel OCR routes now identify fast-path vs RapidOCR escalation, with timing and gate reasons.
- Benchmark now checks key-field parity against forced full ensemble instead of measuring speed alone.

# LableLens v2.3.0 — Adaptive OCR routing

- RapidOCR/ONNX now runs first in the production OCR path.
- High-confidence, high-coverage RapidOCR results skip unnecessary Tesseract work.
- Missing evidence triggers targeted full/bottom/top Tesseract recovery; raw PSM 3 is last-resort only.
- Tesseract fallback uses one subprocess invocation per pass and an 8-second configurable timeout.
- Added `adaptive`, `ensemble`, `rapid_only`, and `tesseract_only` modes for deployment and regression testing.
- `/health` exposes OCR routing mode and thresholds; inspection responses expose per-panel routing and timing diagnostics.
- Updated UI copy/cache to describe the actual adaptive pipeline.

# LableLens platform rebuild — 2026-09-10

This pass turns the earlier presentation-heavy build into an evidence-led platform whose UI claims match its backend behaviour.

Highlights include tri-state verdicts, complete six-group result aggregation, manufacturer/address and consumer-care sub-checks, unit-sale-price screening, calibrated font checks, portable RapidOCR/Tesseract setup, genuine 24-hour signed guest workspaces with scoped history, real reports, image hashes plus per-session record chaining, barcode/QR decoding, e-commerce text screening, multi-panel capture, confirmed-only catalogue learning, corrected expiry-month handling, and a redesigned overview/inspection experience.

The Legal Metrology engine remains intentionally conservative: incomplete evidence and category-specific uncertainty resolve to **Needs review** rather than an unsupported green/red legal conclusion.


## 2.5.0 - Presentation polish
- Frontend-only visual and interaction polish for inspector and admin workspaces.
- Added real-data KPI animations, attention queue, live refresh state, chart reveal animation and theme controls.
- Added smoother workspace transitions, scan visualization motion and responsive presentation improvements.
- OCR engine, routing, thresholds, preprocessing and backend compliance logic were not changed.
