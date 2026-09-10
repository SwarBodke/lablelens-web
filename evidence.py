"""Evidence preservation and hash-chain helpers.

Each uploaded image gets a SHA-256 digest.  Inspection records can also be linked
with a per-session SHA-256 chain: every record hash commits to the previous hash
plus a canonical subset of the current inspection.  This is tamper-evident within
the stored history, but it is not an externally anchored signature or proof of
custody by a government authority.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Dict, Any, Optional

BASE_DIR = Path(__file__).resolve().parent
EVIDENCE_DIR = Path(os.getenv("EVIDENCE_DIR") or (BASE_DIR / "evidence"))
PRESERVE_EVIDENCE = os.getenv("PRESERVE_EVIDENCE", "true").lower() in {"1", "true", "yes"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def chained_record_hash(previous_hash: Optional[str], payload: Dict[str, Any]) -> str:
    material = (previous_hash or "GENESIS") + "\n" + canonical_json(payload)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _safe_session_dir(session_id: str) -> Path:
    safe = "".join(c for c in (session_id or "anonymous") if c.isalnum() or c in "-_")[:80] or "anonymous"
    p = EVIDENCE_DIR / safe
    p.mkdir(parents=True, exist_ok=True)
    return p


def preserve_image(data: bytes, session_id: str, content_type: Optional[str] = None, index: int = 0) -> Dict[str, Any]:
    digest = sha256_bytes(data)
    ext = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/tiff": ".tiff",
    }.get((content_type or "").lower(), ".img")
    record = {
        "sha256": digest,
        "bytes": len(data),
        "content_type": content_type or "application/octet-stream",
        "index": index,
    }
    if PRESERVE_EVIDENCE:
        dest = _safe_session_dir(session_id) / f"{index:02d}-{digest}{ext}"
        if not dest.exists():
            dest.write_bytes(data)
        record["path"] = str(dest)
    return record


def remove_session_evidence(session_id: str) -> bool:
    """Remove preserved originals for an expired guest workspace."""
    safe = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")[:80]
    if not safe:
        return False
    target = EVIDENCE_DIR / safe
    try:
        # Resolve and ensure deletion remains under EVIDENCE_DIR.
        base = EVIDENCE_DIR.resolve()
        resolved = target.resolve()
        if resolved == base or base not in resolved.parents:
            return False
        if resolved.exists():
            shutil.rmtree(resolved)
            return True
    except Exception:
        return False
    return False
