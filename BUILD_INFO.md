# LableLens Complete Website Build

Version: 2.7.0

This package contains the complete deployable LableLens application codebase, including:

- Smart OCR routing (Tesseract-first, targeted recovery, RapidOCR/ONNX escalation, accuracy guard)
- Six-group Legal Metrology compliance engine and inspection contract
- Physical and multi-panel package inspection
- E-commerce inspection
- Google staff authentication with Admin / Officer role mapping
- Guest sessions
- Admin Console and Officer monitoring
- Compliance case / complaint workflow
- Database-backed analytics and dashboard graphs
- Audit trail and system health
- Evidence SHA-256 handling
- PDF / DOCX / JSON reports
- FastAPI backend, frontend, PWA assets, Dockerfile and launch scripts

Validation before packaging:

- Python compilation: PASS
- LableLens smoke/regression tests: PASS
- Management/admin smoke tests: PASS
- Google staff auth tests: PASS
- Frontend JavaScript syntax checks: PASS

Runtime dependencies are installed via requirements.txt and system-level Tesseract must be available on the host.
