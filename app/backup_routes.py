"""Settings → Backup: download a backup, upload one, preview, confirm, self-restart (logic in services/backup.py)."""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from starlette.background import BackgroundTask

from app.i18n import t
from app.services import backup

log = logging.getLogger(__name__)
router = APIRouter()
_restore_lock = asyncio.Lock()


def _back(msg: str) -> RedirectResponse:
    return RedirectResponse(f"/settings?msg={quote(msg, safe='')}#backup", status_code=303)


def panel() -> dict:
    """Template context for the Backup panel on the settings page."""
    return {"crypto": backup.has_crypto(), "safety": backup.safety_backups(), "keep": backup.KEEP_SAFETY}


@router.post("/settings/backup/export")
async def backup_export(include_profile: str | None = Form(None), passphrase: str = Form(""),
                        passphrase2: str = Form("")):
    if passphrase and not backup.has_crypto():
        return _back(t("Encryption is not available in this installation."))
    if passphrase != passphrase2:
        return _back(t("The two passphrases do not match."))
    if passphrase and len(passphrase) < 8:
        return _back(t("Use a passphrase with at least 8 characters."))
    backup.purge_exports()
    name = backup.export_name(encrypted=bool(passphrase))
    dest = backup.backups_dir() / f"export-{name}"
    try:
        await asyncio.to_thread(backup.create_backup, dest, bool(include_profile), passphrase or None)
    except (OSError, backup.BackupError) as e:
        log.exception("backup export failed")
        dest.unlink(missing_ok=True)
        return _back(t("Backup failed: {error}", error=e))
    return FileResponse(dest, media_type="application/octet-stream" if passphrase else "application/zip",
                        filename=name, background=BackgroundTask(dest.unlink, missing_ok=True))


@router.post("/settings/backup/import")
async def backup_import(request: Request, file: UploadFile = File(...), passphrase: str = Form("")):
    from app.main import render
    try:
        pending = await asyncio.to_thread(backup.inspect_upload, file.file, passphrase or None)
    except backup.NeedsPassphrase as e:
        return _back(str(e))
    except backup.BackupError as e:
        return _back(t("Backup rejected: {error}", error=e))
    finally:
        await file.close()
    return render(request, "backup_preview.html", p=pending.summary, token=pending.token, filename=file.filename or "")


@router.get("/settings/backup/preview/{token}")
async def backup_preview(request: Request, token: str):
    from app.main import render
    try:
        pending = await asyncio.to_thread(backup.load_pending, token)
    except (backup.BackupError, OSError, ValueError) as e:
        return _back(str(e) if isinstance(e, backup.BackupError) else t("This import is no longer available. Upload the backup again."))
    return render(request, "backup_preview.html", p=pending.summary, token=token, filename="")


@router.post("/settings/backup/cancel")
async def backup_cancel(token: str = Form("")):
    backup.cancel_pending(token)
    return _back(t("Import cancelled, nothing was changed."))


@router.post("/settings/backup/restore")
async def backup_restore(request: Request, token: str = Form(""), confirm: str | None = Form(None)):
    from app import scheduler
    from app.main import render
    from app.willhaben.session import get_session

    if not confirm:
        return RedirectResponse(f"/settings/backup/preview/{quote(token, safe='')}", status_code=303)
    if _restore_lock.locked():
        return _back(t("An import is already running."))
    async with _restore_lock:
        try:
            pending = await asyncio.to_thread(backup.load_pending, token)
        except (backup.BackupError, OSError, ValueError):
            return _back(t("This import is no longer available. Upload the backup again."))
        # nothing may write to the database or the browser profile while they are replaced
        scheduler.shutdown()
        if pending.summary.get("profile"):
            try:
                await get_session().stop()
            except Exception as e:  # noqa: BLE001 - restore anyway, the restart starts a fresh browser
                log.warning("browser stop before restore failed: %s", e)
        error, safety = None, None
        try:
            res = await asyncio.to_thread(backup.restore, token)
            safety = res["safety"]
        except Exception as e:  # noqa: BLE001 - show it; the restart brings back a consistent process
            log.exception("backup restore failed")
            error = t("Restore failed: {error}", error=e)
    # the old process still holds caches, the scheduler and (maybe) a stopped browser: start fresh in any case
    backup.schedule_restart()            # fires ~2 s later, after this page went out
    return render(request, "backup_restart.html", error=error, safety=safety)


@router.get("/settings/backup/file/{name}")
async def backup_file(name: str):
    p = backup.safety_path(name)
    if not p:
        raise HTTPException(404)
    return FileResponse(p, media_type="application/zip", filename=p.name)
