from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from .auth import get_current_user
from .db import Conversation, Message, User, get_db

router = APIRouter(prefix="/conversations", tags=["conversations"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class ConversationCreate(BaseModel):
    title: str = ""
    merchant_id: Optional[int] = None


class ConversationUpdate(BaseModel):
    title: Optional[str] = None
    is_archived: Optional[bool] = None


class MessagesAppend(BaseModel):
    messages: list[dict]   # [{"role": "user"|"assistant", "content": "..."}]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_conv_or_404(conv_id: int, user: User, db: Session) -> Conversation:
    conv = db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
def list_conversations(
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = (
        select(Conversation)
        .where(Conversation.user_id == user.id, Conversation.is_archived == False)
        .order_by(Conversation.updated_at.desc())
        .offset(offset)
        .limit(limit)
    )
    convs = db.exec(stmt).all()
    return [
        {
            "id": c.id,
            "title": c.title or "新对话",
            "merchant_id": c.merchant_id,
            "created_at": c.created_at.isoformat(),
            "updated_at": c.updated_at.isoformat(),
        }
        for c in convs
    ]


@router.post("", status_code=201)
def create_conversation(
    req: ConversationCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = Conversation(
        user_id=user.id,
        merchant_id=req.merchant_id,
        title=req.title,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return {"id": conv.id, "title": conv.title, "created_at": conv.created_at.isoformat()}


@router.get("/{conv_id}")
def get_conversation(
    conv_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = _get_conv_or_404(conv_id, user, db)
    msgs = db.exec(
        select(Message)
        .where(Message.conversation_id == conv_id)
        .order_by(Message.created_at)
    ).all()
    return {
        "id": conv.id,
        "title": conv.title or "新对话",
        "merchant_id": conv.merchant_id,
        "created_at": conv.created_at.isoformat(),
        "updated_at": conv.updated_at.isoformat(),
        "messages": [{"id": m.id, "role": m.role, "content": m.content, "canvas_json": m.canvas_json, "created_at": m.created_at.isoformat()} for m in msgs],
    }


@router.put("/{conv_id}")
def update_conversation(
    conv_id: int,
    req: ConversationUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = _get_conv_or_404(conv_id, user, db)
    if req.title is not None:
        conv.title = req.title
    if req.is_archived is not None:
        conv.is_archived = req.is_archived
    conv.updated_at = datetime.utcnow()
    db.add(conv)
    db.commit()
    return {"ok": True}


@router.delete("/{conv_id}")
def delete_conversation(
    conv_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = _get_conv_or_404(conv_id, user, db)
    msgs = db.exec(select(Message).where(Message.conversation_id == conv_id)).all()
    for m in msgs:
        db.delete(m)
    db.flush()
    db.delete(conv)
    db.commit()
    return {"ok": True}


@router.post("/{conv_id}/messages")
def append_messages(
    conv_id: int,
    req: MessagesAppend,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = _get_conv_or_404(conv_id, user, db)

    for m in req.messages:
        db.add(Message(
            conversation_id=conv_id,
            role=m["role"],
            content=m["content"],
            canvas_json=m.get("canvas_json"),
        ))

    # Auto-set title from first user message if still empty
    if not conv.title and req.messages:
        first_user = next((m["content"] for m in req.messages if m["role"] == "user"), None)
        if first_user:
            conv.title = first_user[:30]

    conv.updated_at = datetime.utcnow()
    db.add(conv)
    db.commit()
    return {"ok": True}
