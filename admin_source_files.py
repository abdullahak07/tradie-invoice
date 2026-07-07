from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from admin_dashboard import admin_login
from invoice_routes import db

router = APIRouter(prefix="/admin/tester-debug/source", tags=["admin-source-files"])


@router.get("/{user_id}")
def open_source_file(
    user_id: str,
    kind: str = Query(default="source", pattern="^(source|logo|letterhead)$"),
    _: str = Depends(admin_login),
) -> FileResponse:
    with db() as conn:
        session = conn.execute(
            """
            SELECT source_path, logo_path
            FROM onboarding_sessions
            WHERE user_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        branding = conn.execute(
            """
            SELECT logo_path, letterhead_path
            FROM business_branding
            WHERE user_id = ?
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()

    if kind == "source":
        raw_path = str(session["source_path"] or "") if session else ""
    elif kind == "logo":
        raw_path = str((branding["logo_path"] if branding else "") or (session["logo_path"] if session else "") or "")
    else:
        raw_path = str(branding["letterhead_path"] or "") if branding else ""

    if not raw_path:
        raise HTTPException(status_code=404, detail=f"No {kind} file is stored for this user")

    path = Path(raw_path).resolve()
    allowed_root = Path("/app/data/business_branding").resolve()
    if not str(path).startswith(str(allowed_root)):
        raise HTTPException(status_code=403, detail="Stored file is outside the allowed branding directory")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Stored file is no longer present on this Railway instance")

    return FileResponse(path, filename=path.name)
