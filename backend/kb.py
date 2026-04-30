from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
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
    import sys
    _main = sys.modules.get("backend.main") or sys.modules.get("app.backend.main")
    if _main and hasattr(_main, "DATA_SESSIONS"):
        session_key = f"user:{user.id}"
        session = _main.DATA_SESSIONS.pop(session_key, None)
        if session:
            try:
                session["conn"].close()
            except Exception:
                pass

    db.delete(doc)
    db.commit()
    return {"ok": True, "chunks_removed": deleted}


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
