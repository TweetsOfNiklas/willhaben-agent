"""Batch ad creation: a dump of photos of many articles -> Claude sorts them into products -> user corrects
-> one AdDraft per group through the normal pipeline (ad_creator.create_draft)."""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any, Coroutine

from sqlmodel import select

from app.config import get_settings
from app.db import AdBatch, session
from app.i18n import t
from app.llm.base import LLMError, get_backend
from app.llm.prompts import SORT_SCHEMA, SORT_SYSTEM, sort_prompt
from app.services import ad_creator
from app.services import timeline

log = logging.getLogger(__name__)

MAX_BATCH_IMAGES = 60
CREATE_PARALLEL = 2          # drafts written at the same time (each is 1-2 Claude calls + a willhaben search)
_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> None:
    """Run a slow job in the background; the page polls the batch status."""
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def get(batch_id: int) -> AdBatch | None:
    with session() as db:
        return db.get(AdBatch, batch_id)


def _update(batch_id: int, **fields: Any) -> AdBatch:
    with session() as db:
        b = db.get(AdBatch, batch_id)
        for k, v in fields.items():
            setattr(b, k, v)
        db.add(b)
        db.commit()
        db.refresh(b)
    return b


def _folder(batch_id: int) -> Path:
    return get_settings().uploads_dir / f"batch-{batch_id}"


def new_batch(notes: str) -> AdBatch:
    with session() as db:
        b = AdBatch(notes=notes.strip())
        db.add(b)
        db.commit()
        db.refresh(b)
    return b


def add_image(batch_id: int, name: str, data: bytes) -> bool:
    """Store one uploaded photo (called per file so a big upload never sits in memory at once)."""
    b = get(batch_id)
    ext = Path(name or "").suffix.lower()
    if len(b.image_paths) >= MAX_BATCH_IMAGES or ext not in ad_creator.ALLOWED or not data \
            or len(data) > ad_creator.MAX_IMAGE_BYTES:
        return False
    folder = _folder(batch_id)
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{len(b.image_paths) + 1:02d}{ext}"
    p.write_bytes(data)
    _update(batch_id, image_paths=[*b.image_paths, str(p)])
    return True


def locked(g: dict[str, Any]) -> bool:
    """A group that already became a draft is no longer changed by sorting or review."""
    return bool(g.get("draft_id"))


def unassigned(b: AdBatch) -> list[int]:
    used = {i for g in b.groups for i in g["photos"]}
    return [i for i in range(len(b.image_paths)) if i not in used]


def _next_gid(groups: list[dict[str, Any]]) -> str:
    n = max((int(g["id"][1:]) for g in groups if g["id"][1:].isdigit()), default=0)
    return f"g{n + 1}"


def _new_group(gid: str, photos: list[int], **fields: Any) -> dict[str, Any]:
    return {"id": gid, "photos": photos, "product": "", "product_query": "", "observations": "",
            "confidence": "", "question": "", "comment": "", "include": True, "draft_id": None, "error": None,
            **fields}


def groups_from_result(data: dict[str, Any], pool: list[int], keep: list[dict[str, Any]],
                       previous: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Claude's 1-based photo numbers (over `pool`) -> groups of image indices; each photo at most once.
    The user's comment and include flag move to the new group sharing the most photos with the old one."""
    groups, seen, carried = list(keep), set(), set()
    previous = [g for g in previous or [] if not locked(g)]
    for raw in data.get("groups") or []:
        photos = []
        for n in raw.get("photos") or []:
            if isinstance(n, int) and 1 <= n <= len(pool) and pool[n - 1] not in seen:
                seen.add(pool[n - 1])
                photos.append(pool[n - 1])
        if not photos:
            continue
        old = max((g for g in previous if g["id"] not in carried), default=None,
                  key=lambda g: len(set(g["photos"]) & set(photos)))
        user = {}
        if old and set(old["photos"]) & set(photos):
            carried.add(old["id"])
            user = {"comment": old.get("comment") or "", "include": old.get("include", True)}
        groups.append(_new_group(
            _next_gid(groups), photos, product=(raw.get("product") or "").strip(),
            product_query=(raw.get("product_query") or "").strip(),
            observations=(raw.get("observations") or "").strip(), confidence=raw.get("confidence") or "",
            question=(raw.get("question") or "").strip(), **user))
    return groups


@timeline.scoped(batch_id="batch_id")
async def sort(batch_id: int, hint: str | None = None) -> AdBatch:
    """Let Claude (re-)group the photos. Groups that are drafts already stay as they are."""
    b = get(batch_id)
    keep = [g for g in b.groups if locked(g)]
    taken = {i for g in keep for i in g["photos"]}
    pool = [i for i in range(len(b.image_paths)) if i not in taken]
    if not pool:
        return _update(batch_id, status="review", error=None)
    pos = {img: n for n, img in enumerate(pool, 1)}
    current = [{"photos": [pos[i] for i in g["photos"]], "product": g.get("product"), "comment": g.get("comment")}
               for g in b.groups if not locked(g) and g["photos"]]
    _update(batch_id, status="sorting", error=None)
    backend = get_backend("ads")
    if hasattr(backend, "timeout_s"):   # Claude looks at every photo; give it time
        backend.timeout_s = max(backend.timeout_s, 120 + 10 * len(pool))
    try:
        result = await backend.complete(
            system=SORT_SYSTEM, prompt=sort_prompt(notes=b.notes, n_images=len(pool), current=current, hint=hint),
            schema=SORT_SCHEMA, images=[Path(b.image_paths[i]) for i in pool])
    except LLMError as e:
        return _update(batch_id, status="error", error=t("Sorting failed: {error}", error=e))
    except Exception as e:  # noqa: BLE001  (background task: never leave the batch stuck in "sorting")
        log.exception("batch %s: sorting crashed", batch_id)
        return _update(batch_id, status="error", error=t("Sorting failed: {error}", error=e))
    groups = groups_from_result(result.data, pool, keep, b.groups)
    return _update(batch_id, groups=groups, status="review", note=(result.data.get("note") or "").strip() or None,
                   model=result.model)


def apply_review(batch_id: int, order: list[tuple[int, str]], fields: dict[str, dict[str, Any]]) -> AdBatch:
    """Save the user's corrections.

    order:  (image index, target) in page order; target is a group id, "new" (all such photos form one new group)
            or "" (not used). Photo order inside a group follows page order, so the first one is the cover.
    fields: group id -> {product, comment, include}
    """
    b = get(batch_id)
    groups = {g["id"]: dict(g, photos=list(g["photos"]) if locked(g) else []) for g in b.groups}
    taken = {i for g in groups.values() if locked(g) for i in g["photos"]}
    new_gid = None
    for idx, target in order:
        if idx in taken or not 0 <= idx < len(b.image_paths):
            continue
        if target == "new":
            if new_gid is None:
                new_gid = _next_gid(list(groups.values()))
                groups[new_gid] = _new_group(new_gid, [])
            target = new_gid
        g = groups.get(target)
        if g is None or locked(g):
            continue
        g["photos"].append(idx)
        taken.add(idx)
    for gid, f in fields.items():
        g = groups.get(gid)
        if g is None or locked(g):
            continue
        g["product"] = (f.get("product") or "").strip()
        g["comment"] = (f.get("comment") or "").replace("\r\n", "\n").strip()
        g["include"] = bool(f.get("include"))
    kept = [g for g in groups.values() if g["photos"] or locked(g)]
    return _update(batch_id, groups=kept)


def group_notes(b: AdBatch, g: dict[str, Any]) -> str:
    """Input text for the normal draft pipeline: the user's words first, Claude's sorting result as hints."""
    parts = []
    if g.get("comment"):
        parts.append(g["comment"])
    if g.get("product"):
        parts.append(f"Artikel: {g['product']}")
    if g.get("observations"):
        parts.append(f"Auf den Fotos erkannt (automatisch, bitte prüfen): {g['observations']}")
    if b.notes:
        parts.append(f"Gilt für alle meine Artikel: {b.notes}")
    return "\n".join(parts)


def pending(b: AdBatch) -> list[dict[str, Any]]:
    return [g for g in b.groups if g.get("include") and not locked(g) and g["photos"]]


async def create_drafts(batch_id: int) -> AdBatch:
    """One AdDraft per included group, through the same pipeline as a single new ad."""
    b = _update(batch_id, status="creating", error=None)
    sem = asyncio.Semaphore(CREATE_PARALLEL)

    async def one(g: dict[str, Any]) -> None:
        async with sem:
            files = [(Path(b.image_paths[i]).name, Path(b.image_paths[i]).read_bytes())
                     for i in g["photos"][:ad_creator.MAX_IMAGES]]
            try:
                d = await ad_creator.create_draft(group_notes(b, g), files, batch_id=batch_id)
                _set_group(batch_id, g["id"], draft_id=d.id, error=d.error)
            except Exception as e:  # noqa: BLE001
                log.exception("batch %s group %s: draft failed", batch_id, g["id"])
                _set_group(batch_id, g["id"], error=str(e))

    await asyncio.gather(*(one(g) for g in pending(b)))
    return _update(batch_id, status="review")


def _set_group(batch_id: int, gid: str, **fields: Any) -> None:
    b = get(batch_id)
    _update(batch_id, groups=[dict(g, **fields) if g["id"] == gid else g for g in b.groups])


def recover() -> None:
    """After a restart no background job is running any more."""
    with session() as db:
        for b in db.exec(select(AdBatch).where(AdBatch.status.in_(["sorting", "creating"]))):
            if b.status == "sorting":
                b.status, b.error = "error", t("Sorting was interrupted by a restart.")
            else:
                b.status = "review"
            db.add(b)
        db.commit()


def delete(batch_id: int) -> None:
    """Drafts keep their own copies of the photos."""
    with session() as db:
        b = db.get(AdBatch, batch_id)
        if b:
            db.delete(b)
            db.commit()
    shutil.rmtree(_folder(batch_id), ignore_errors=True)
