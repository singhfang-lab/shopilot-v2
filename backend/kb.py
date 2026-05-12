from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, select

from .auth import get_current_user, get_merchant_for_user
from .db import KBDocument, PlatformKBDocument, User, UserMerchant, get_db
from . import rag

router = APIRouter(prefix="/kb", tags=["kb"])


def _merge_overlapping_chunks(chunks: list[str]) -> str:
    """Re-order and merge chunks that were split with character-level overlap."""
    if not chunks:
        return ""
    if len(chunks) == 1:
        return chunks[0]

    # Sort chunks by finding which follows which via overlap
    # Start with the chunk that is not a suffix of any other chunk
    def overlap_score(a: str, b: str) -> int:
        """How many chars of b's start appear at the end of a."""
        limit = min(120, len(a), len(b))
        for n in range(limit, 9, -1):
            if a.endswith(b[:n]):
                return n
        return 0

    # Find the first chunk: one that no other chunk leads into
    remaining = list(chunks)
    # Pick the one containing a title marker if possible, else longest
    first = next((c for c in remaining if c.lstrip().startswith('#')), None)
    if first is None:
        first = max(remaining, key=len)
    remaining.remove(first)

    ordered = [first]
    while remaining:
        last = ordered[-1]
        # Find best next chunk
        best_chunk = max(remaining, key=lambda c: overlap_score(last, c))
        ordered.append(best_chunk)
        remaining.remove(best_chunk)

    # Merge with overlap removal
    result = ordered[0]
    for nxt in ordered[1:]:
        limit = min(120, len(result), len(nxt))
        best = 0
        for n in range(limit, 9, -1):
            if result.endswith(nxt[:n]):
                best = n
                break
        result = result + "\n" + nxt[best:]

    # Join continuation lines: a line that doesn't start with *, #, -, or whitespace
    # and follows a line ending mid-sentence (no sentence-ending punctuation) is a continuation
    lines = result.split("\n")
    joined: list[str] = []
    for line in lines:
        is_continuation = (
            joined
            and line
            and not line[0] in ("#", "*", "-", " ", "\t")
            and joined[-1]
            and joined[-1][-1] not in ("。", ".", "！", "？", "：", ":", "\n")
        )
        if is_continuation:
            joined[-1] = joined[-1] + line
        else:
            joined.append(line)

    return "\n".join(joined)

UPLOADS_DIR = Path.home() / "usb-assistant" / "uploads"


def _shop_id(user_id: int, merchant_id: int) -> str:
    return f"u{user_id}_m{merchant_id}"


# ── Merchant KB ───────────────────────────────────────────────────────────────

@router.get("/documents")
async def list_kb_documents(
    merchant_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    merchant = get_merchant_for_user(user, db)
    if not merchant:
        return []

    mid = merchant.id if user.role != "admin" else (merchant_id or merchant.id)

    # Find the user_id that owns this merchant (for shop_id key)
    um = db.exec(select(UserMerchant).where(UserMerchant.merchant_id == mid)).first()
    owner_uid = um.user_id if um else user.id
    shop_id = _shop_id(owner_uid, mid)

    # DB records (uploaded files)
    docs = db.exec(select(KBDocument).where(KBDocument.merchant_id == mid)).all()
    doc_map = {d.filename: d for d in docs}

    # Live sources from ChromaDB (includes onboarding-generated KB)
    chroma_sources = {s["source"]: s["chunk_count"] for s in await rag.list_sources_async(shop_id)}

    results = []

    # DB-backed documents (uploaded files)
    for d in docs:
        results.append({
            "id": d.id,
            "original_name": d.original_name,
            "filename": d.filename,
            "source_type": "upload",
            "status": d.status,
            "chunk_count": chroma_sources.get(d.filename, d.chunk_count),
            "file_size": d.file_size,
            "uploaded_at": d.uploaded_at.isoformat(),
        })

    # ChromaDB-only sources (onboarding-generated, not in DB)
    db_filenames = {d.filename for d in docs}
    for source, chunk_count in chroma_sources.items():
        if source not in db_filenames:
            results.append({
                "id": f"chroma:{source}",
                "original_name": source.replace("onboarding:", "品牌初始知识库："),
                "filename": source,
                "source_type": "generated",
                "status": "indexed",
                "chunk_count": chunk_count,
                "file_size": 0,
                "uploaded_at": None,
            })

    return results


@router.delete("/documents/{doc_id}")
def delete_kb_document(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    merchant = get_merchant_for_user(user, db)
    if not merchant:
        raise HTTPException(status_code=403, detail="No merchant")

    um = db.exec(select(UserMerchant).where(UserMerchant.merchant_id == merchant.id)).first()
    owner_uid = um.user_id if um else user.id
    shop_id = _shop_id(owner_uid, merchant.id)

    # ChromaDB-only source (generated)
    if str(doc_id).startswith("chroma:"):
        source = str(doc_id)[7:]
        deleted = rag.delete_by_source(source, shop_id)
        return {"ok": True, "chunks_removed": deleted}

    # DB-backed document
    doc = db.get(KBDocument, int(doc_id))
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.merchant_id != merchant.id:
        raise HTTPException(status_code=403, detail="Access denied")

    deleted = rag.delete_by_source(doc.filename, shop_id)

    file_path = UPLOADS_DIR / f"m{doc.merchant_id}" / doc.filename
    if file_path.exists():
        file_path.unlink()

    # Clear DuckDB session so deleted file is no longer queryable
    from . import session_store as _ss
    _ss.pop_session(f"user:{user.id}")

    db.delete(doc)
    db.commit()
    return {"ok": True, "chunks_removed": deleted}


# ── Merchant KB folder management ────────────────────────────────────────────

class CreateFolderRequest(BaseModel):
    folder_path: str   # e.g. "销售数据" or "2024/Q1"

class RenameRequest(BaseModel):
    new_name: str


@router.post("/folders")
def create_kb_folder(
    req: CreateFolderRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    merchant = get_merchant_for_user(user, db)
    if not merchant:
        raise HTTPException(403, "No merchant")

    folder_path = req.folder_path.strip("/")
    if not folder_path:
        raise HTTPException(400, "folder_path is required")

    placeholder_name = f"{folder_path}/.keep"
    existing = db.exec(
        select(KBDocument).where(
            KBDocument.merchant_id == merchant.id,
            KBDocument.original_name == placeholder_name,
        )
    ).first()
    if existing:
        return {"id": existing.id, "original_name": existing.original_name}

    doc = KBDocument(
        merchant_id=merchant.id,
        filename=f".keep_{hashlib.md5(placeholder_name.encode()).hexdigest()[:8]}",
        original_name=placeholder_name,
        status="indexed",
        chunk_count=0,
        file_size=0,
        uploaded_by=user.id,
        indexed_at=datetime.now(timezone.utc),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return {"id": doc.id, "original_name": doc.original_name}


@router.patch("/documents/{doc_id}/rename")
def rename_kb_document(
    doc_id: int,
    req: RenameRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    merchant = get_merchant_for_user(user, db)
    if not merchant:
        raise HTTPException(403, "No merchant")

    doc = db.get(KBDocument, doc_id)
    if not doc or doc.merchant_id != merchant.id:
        raise HTTPException(404, "Document not found")

    # Preserve folder prefix
    parts = doc.original_name.rsplit("/", 1)
    if len(parts) == 2:
        doc.original_name = f"{parts[0]}/{req.new_name}"
    else:
        doc.original_name = req.new_name

    db.add(doc)
    db.commit()
    return {"id": doc.id, "original_name": doc.original_name}


@router.post("/folders/rename")
def rename_kb_folder(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    old_path: str = Form(...),
    new_name: str = Form(...),
):
    """Rename a folder by updating all documents whose original_name starts with old_path/."""
    merchant = get_merchant_for_user(user, db)
    if not merchant:
        raise HTTPException(403, "No merchant")

    old_path = old_path.strip("/")
    new_name = new_name.strip("/")
    # Compute new path: replace last segment
    parent = old_path.rsplit("/", 1)[0] if "/" in old_path else ""
    new_path = f"{parent}/{new_name}".strip("/")

    docs = db.exec(select(KBDocument).where(KBDocument.merchant_id == merchant.id)).all()
    updated = 0
    for doc in docs:
        if doc.original_name == old_path or doc.original_name.startswith(old_path + "/"):
            doc.original_name = new_path + doc.original_name[len(old_path):]
            db.add(doc)
            updated += 1
    db.commit()
    return {"ok": True, "updated": updated}


@router.delete("/folders")
def delete_kb_folder(
    folder_path: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete all documents inside a folder (including .keep placeholder)."""
    merchant = get_merchant_for_user(user, db)
    if not merchant:
        raise HTTPException(403, "No merchant")

    um = db.exec(select(UserMerchant).where(UserMerchant.merchant_id == merchant.id)).first()
    owner_uid = um.user_id if um else user.id
    shop_id = _shop_id(owner_uid, merchant.id)

    folder_path = folder_path.strip("/")
    docs = db.exec(select(KBDocument).where(KBDocument.merchant_id == merchant.id)).all()
    removed = 0
    for doc in docs:
        if doc.original_name == folder_path or doc.original_name.startswith(folder_path + "/"):
            rag.delete_by_source(doc.filename, shop_id)
            file_path = UPLOADS_DIR / f"m{doc.merchant_id}" / doc.filename
            if file_path.exists():
                file_path.unlink()
            db.delete(doc)
            removed += 1
    db.commit()
    return {"ok": True, "removed": removed}


# ── Brand brief ──────────────────────────────────────────────────────────────

@router.get("/brief")
async def get_brand_brief(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the full text of the onboarding-generated brand brief."""
    merchant = get_merchant_for_user(user, db)
    if not merchant:
        raise HTTPException(status_code=404, detail="No merchant")

    um = db.exec(select(UserMerchant).where(UserMerchant.merchant_id == merchant.id)).first()
    owner_uid = um.user_id if um else user.id
    shop_id = _shop_id(owner_uid, merchant.id)

    try:
        chunks = await rag.get_onboarding_chunks_async(shop_id)
        if not chunks:
            return {"brand_name": merchant.name, "text": "", "chunk_count": 0}
        merged = _merge_overlapping_chunks(chunks)
        return {"brand_name": merchant.name, "text": merged, "chunk_count": len(chunks)}
    except Exception:
        return {"brand_name": merchant.name, "text": "", "chunk_count": 0}


# ── Platform KB (read-only for users) ────────────────────────────────────────

@router.get("/platform")
async def list_platform_kb(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    docs = db.exec(select(PlatformKBDocument)).all()
    sources = {s["source"]: s["chunk_count"] for s in await rag.list_sources_async(None)}

    result = [
        {
            "id": d.id,
            "original_name": d.original_name,
            "status": d.status,
            "chunk_count": sources.get(d.filename, d.chunk_count),
            "uploaded_at": d.uploaded_at.isoformat(),
        }
        for d in docs
    ]

    # Surface ChromaDB-only sources not tracked in DB
    tracked = {d.filename for d in docs}
    for source, chunk_count in sources.items():
        if source not in tracked:
            result.append({
                "id": f"chroma:{source}",
                "original_name": source,
                "status": "indexed",
                "chunk_count": chunk_count,
                "uploaded_at": None,
            })

    return result
