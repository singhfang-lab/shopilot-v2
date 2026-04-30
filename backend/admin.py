from __future__ import annotations

import asyncio
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from .auth import get_merchant_for_user, require_admin
from .db import (
    KBDocument, LLMConfig, Merchant, PlatformKBDocument, SystemPrompt,
    User, UserMerchant, get_db,
)
from . import rag, llm as llm_module
from .prompts import SYSTEM_PROMPT

router = APIRouter(prefix="/admin", tags=["admin"])

UPLOADS_DIR = Path.home() / "usb-assistant" / "uploads"
PLATFORM_UPLOADS_DIR = UPLOADS_DIR / "platform"
CLAUDE_API_BASE = "https://api.anthropic.com/v1"
CLAUDE_MODEL = "claude-sonnet-4-6"


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats")
def get_stats(admin=Depends(require_admin), db: Session = Depends(get_db)):
    user_count = len(db.exec(select(User)).all())
    merchant_count = len(db.exec(select(Merchant)).all())
    doc_count = len(db.exec(select(KBDocument)).all())
    platform_doc_count = len(db.exec(select(PlatformKBDocument)).all())
    active_prompt = db.exec(
        select(SystemPrompt).where(SystemPrompt.status == "active")
    ).first()
    return {
        "user_count": user_count,
        "merchant_count": merchant_count,
        "total_docs": doc_count,
        "platform_docs": platform_doc_count,
        "active_prompt_version": active_prompt.version if active_prompt else None,
        "active_prompt_label": active_prompt.label if active_prompt else "默认内置",
    }


# ── User management ───────────────────────────────────────────────────────────

@router.get("/users")
def list_users(admin=Depends(require_admin), db: Session = Depends(get_db)):
    users = db.exec(select(User)).all()
    result = []
    for u in users:
        merchant = get_merchant_for_user(u, db)
        result.append({
            "id": u.id,
            "email": u.email,
            "display_name": u.display_name,
            "role": u.role,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat(),
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "merchant": {"id": merchant.id, "name": merchant.name} if merchant else None,
        })
    return result


@router.post("/users/{user_id}/toggle-active")
def toggle_user_active(
    user_id: int,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot disable yourself")
    user.is_active = not user.is_active
    db.add(user)
    db.commit()
    return {"id": user.id, "is_active": user.is_active}


@router.post("/users/{user_id}/reset-password")
def reset_user_password(
    user_id: int,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    from .auth import _hash_password
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    tmp_password = secrets.token_urlsafe(8)
    user.password_hash = _hash_password(tmp_password)
    db.add(user)
    db.commit()
    return {"tmp_password": tmp_password}


# ── Platform KB ───────────────────────────────────────────────────────────────

@router.get("/platform-kb")
async def list_platform_kb(admin=Depends(require_admin), db: Session = Depends(get_db)):
    docs = db.exec(select(PlatformKBDocument)).all()
    sources = {s["source"]: s["chunk_count"] for s in await rag.list_sources_async(None)}
    return [
        {
            "id": d.id,
            "original_name": d.original_name,
            "filename": d.filename,
            "status": d.status,
            "chunk_count": sources.get(d.filename, d.chunk_count),
            "uploaded_at": d.uploaded_at.isoformat(),
            "indexed_at": d.indexed_at.isoformat() if d.indexed_at else None,
        }
        for d in docs
    ]


@router.post("/platform-kb/upload")
async def upload_platform_kb(
    file: UploadFile = File(...),
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    PLATFORM_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    bare_name = Path(file.filename).name if file.filename else "upload"
    safe_name = f"{secrets.token_hex(6)}_{bare_name}"
    save_path = PLATFORM_UPLOADS_DIR / safe_name
    content = await file.read()
    save_path.write_bytes(content)

    doc = PlatformKBDocument(
        filename=safe_name,
        original_name=file.filename or safe_name,
        status="processing",
        uploaded_by=admin.id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    # Background indexing
    asyncio.create_task(_index_platform_doc(doc.id, save_path))
    return {"id": doc.id, "original_name": doc.original_name, "status": "processing"}


def _read_file_chunks(path: Path) -> list[str]:
    """Read a file and return text chunks."""
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md"):
        text = path.read_text(encoding="utf-8", errors="ignore")
    elif suffix == ".pdf":
        try:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        except Exception:
            text = ""
    else:
        text = path.read_text(encoding="utf-8", errors="ignore")
    return rag._chunk_text(text) if text.strip() else []


async def _index_platform_doc(doc_id: int, path: Path):
    from .db import engine
    from sqlmodel import Session as S
    try:
        chunks = await asyncio.get_event_loop().run_in_executor(None, _read_file_chunks, path)
        if rag._use_pgvector():
            count = await rag._pg_add_async(chunks, source=path.name, shop_id=None)
        else:
            count = await asyncio.get_event_loop().run_in_executor(None, rag._chroma_add, chunks, path.name, None)
        with S(engine) as db:
            doc = db.get(PlatformKBDocument, doc_id)
            if doc:
                doc.status = "indexed"
                doc.chunk_count = count
                doc.indexed_at = datetime.now(timezone.utc)
                db.add(doc)
                db.commit()
    except Exception as e:
        print(f"[platform-kb] indexing failed: {e}")
        from .db import engine
        from sqlmodel import Session as S2
        with S2(engine) as db:
            doc = db.get(PlatformKBDocument, doc_id)
            if doc:
                doc.status = "failed"
                db.add(doc)
                db.commit()


@router.delete("/platform-kb/{doc_id}")
async def delete_platform_kb(
    doc_id: int,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    doc = db.get(PlatformKBDocument, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    if rag._use_pgvector():
        deleted = await rag._pg_delete_by_source_async(doc.filename, shop_id=None)
    else:
        deleted = rag.delete_by_source(doc.filename, shop_id=None)
    path = PLATFORM_UPLOADS_DIR / doc.filename
    if path.exists():
        path.unlink()
    db.delete(doc)
    db.commit()
    return {"ok": True, "chunks_removed": deleted}


# ── Prompt management ─────────────────────────────────────────────────────────

@router.get("/prompts")
def list_prompts(admin=Depends(require_admin), db: Session = Depends(get_db)):
    prompts = db.exec(select(SystemPrompt).order_by(SystemPrompt.version.desc())).all()
    return [
        {
            "id": p.id,
            "version": p.version,
            "label": p.label,
            "status": p.status,
            "created_at": p.created_at.isoformat(),
            "published_at": p.published_at.isoformat() if p.published_at else None,
            "content_preview": p.content[:120] + "..." if len(p.content) > 120 else p.content,
        }
        for p in prompts
    ]


@router.get("/prompts/active")
def get_active_prompt(admin=Depends(require_admin), db: Session = Depends(get_db)):
    p = db.exec(select(SystemPrompt).where(SystemPrompt.status == "active")).first()
    if not p:
        return {"content": SYSTEM_PROMPT, "version": None, "label": "默认内置", "status": "active"}
    return {"id": p.id, "version": p.version, "label": p.label, "status": p.status, "content": p.content}


@router.get("/prompts/{prompt_id}")
def get_prompt(prompt_id: int, admin=Depends(require_admin), db: Session = Depends(get_db)):
    p = db.get(SystemPrompt, prompt_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")
    return {
        "id": p.id, "version": p.version, "label": p.label,
        "status": p.status, "content": p.content,
        "test_result": p.test_result,
        "created_at": p.created_at.isoformat(),
        "published_at": p.published_at.isoformat() if p.published_at else None,
    }


class CreatePromptRequest(BaseModel):
    content: str
    label: str = ""


@router.post("/prompts")
def create_prompt(
    req: CreatePromptRequest,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    latest = db.exec(select(SystemPrompt).order_by(SystemPrompt.version.desc())).first()
    next_version = (latest.version + 1) if latest else 1
    p = SystemPrompt(
        version=next_version,
        content=req.content,
        label=req.label or f"V{next_version}",
        status="draft",
        created_by=admin.id,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return {"id": p.id, "version": p.version, "label": p.label, "status": p.status}


class UpdatePromptRequest(BaseModel):
    content: str
    label: str = ""


@router.patch("/prompts/{prompt_id}")
def update_prompt(
    prompt_id: int,
    req: UpdatePromptRequest,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    p = db.get(SystemPrompt, prompt_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")
    if p.status not in ("draft", "testing"):
        raise HTTPException(status_code=400, detail="Only draft/testing prompts can be edited")
    p.content = req.content
    if req.label:
        p.label = req.label
    db.add(p)
    db.commit()
    return {"ok": True}


class AISuggestRequest(BaseModel):
    current_content: str
    instruction: str


@router.post("/prompts/ai-suggest")
async def ai_suggest_prompt(
    req: AISuggestRequest,
    admin=Depends(require_admin),
):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="Anthropic API key not configured")

    meta_prompt = f"""你是一位 AI Prompt 工程专家。请根据以下优化指令，对现有 System Prompt 进行改写。

优化指令：{req.instruction}

现有 Prompt：
{req.current_content}

请直接输出改写后的完整 Prompt 文本，不要加任何前缀说明。然后在最后另起一行输出：
---EXPLANATION---
（简短说明你做了哪些改动及原因，2-3句话即可）"""

    async with httpx.AsyncClient(timeout=60.0) as client:
        res = await client.post(
            f"{CLAUDE_API_BASE}/messages",
            json={"model": CLAUDE_MODEL, "max_tokens": 4096, "temperature": 0.4,
                  "messages": [{"role": "user", "content": meta_prompt}]},
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
    if res.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Claude error {res.status_code}")

    text = res.json()["content"][0]["text"]
    if "---EXPLANATION---" in text:
        parts = text.split("---EXPLANATION---", 1)
        return {"suggested_content": parts[0].strip(), "explanation": parts[1].strip()}
    return {"suggested_content": text.strip(), "explanation": ""}


class TestPromptRequest(BaseModel):
    test_message: str


@router.post("/prompts/{prompt_id}/test")
async def test_prompt(
    prompt_id: int,
    req: TestPromptRequest,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    p = db.get(SystemPrompt, prompt_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="Anthropic API key not configured")

    async with httpx.AsyncClient(timeout=60.0) as client:
        res = await client.post(
            f"{CLAUDE_API_BASE}/messages",
            json={"model": CLAUDE_MODEL, "max_tokens": 800, "temperature": 0.5,
                  "system": p.content,
                  "messages": [{"role": "user", "content": req.test_message}]},
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
    if res.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Claude error {res.status_code}")

    reply = res.json()["content"][0]["text"]
    p.test_result = json.dumps({"message": req.test_message, "reply": reply}, ensure_ascii=False)
    p.status = "testing"
    db.add(p)
    db.commit()
    return {"reply": reply}


@router.post("/prompts/{prompt_id}/publish")
def publish_prompt(
    prompt_id: int,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    p = db.get(SystemPrompt, prompt_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")

    # Archive current active
    current_active = db.exec(select(SystemPrompt).where(SystemPrompt.status == "active")).all()
    for old in current_active:
        old.status = "archived"
        db.add(old)

    p.status = "active"
    p.published_at = datetime.now(timezone.utc)
    db.add(p)
    db.commit()
    return {"ok": True, "version": p.version}


@router.post("/prompts/{prompt_id}/rollback")
def rollback_prompt(
    prompt_id: int,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    return publish_prompt(prompt_id, admin, db)


# ── LLM config management ─────────────────────────────────────────────────────

@router.get("/llm-configs")
def list_llm_configs(admin=Depends(require_admin), db: Session = Depends(get_db)):
    configs = db.exec(select(LLMConfig)).all()
    env_keys = {
        "gemini": os.environ.get("GEMINI_API_KEY", ""),
        "claude": os.environ.get("ANTHROPIC_API_KEY", ""),
        "openai": os.environ.get("OPENAI_API_KEY", ""),
    }
    return [
        {
            "id": c.id,
            "provider": c.provider,
            "model": c.model or llm_module.DEFAULT_MODELS.get(c.provider, ""),
            "label": c.label,
            "is_active": c.is_active,
            "has_key": bool(c.api_key) or bool(env_keys.get(c.provider, "")),
            "updated_at": c.updated_at.isoformat(),
        }
        for c in configs
    ]


class LLMConfigUpdateRequest(BaseModel):
    api_key: str = ""
    label: str = ""
    model: str = ""


@router.patch("/llm-configs/{config_id}")
def update_llm_config(
    config_id: int,
    req: LLMConfigUpdateRequest,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    cfg = db.get(LLMConfig, config_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Not found")
    if req.api_key:
        cfg.api_key = req.api_key
    if req.label:
        cfg.label = req.label
    if req.model:
        cfg.model = req.model
    cfg.updated_at = datetime.now(timezone.utc)
    db.add(cfg)
    db.commit()
    return {"ok": True}


@router.post("/llm-configs/{config_id}/toggle-enabled")
def toggle_llm_config_enabled(
    config_id: int,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Toggle whether this config appears in the frontend model selector."""
    cfg = db.get(LLMConfig, config_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Not found")
    cfg.is_active = not cfg.is_active
    cfg.updated_at = datetime.now(timezone.utc)
    db.add(cfg)
    db.commit()
    return {"ok": True, "is_active": cfg.is_active}


@router.delete("/llm-configs/{config_id}")
def delete_llm_config(
    config_id: int,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    cfg = db.get(LLMConfig, config_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(cfg)
    db.commit()
    return {"ok": True}
