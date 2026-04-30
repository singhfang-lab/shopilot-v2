from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from sqlmodel import Field, Session, SQLModel, create_engine

load_dotenv(Path(__file__).parent / ".env")


def _build_engine():
    """Use DATABASE_URL (PostgreSQL) if set, else local SQLite."""
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url:
        # asyncpg DSN → sync psycopg2 for SQLModel
        sync_url = db_url.replace("postgresql+asyncpg://", "postgresql://").replace("postgres://", "postgresql://")
        return create_engine(sync_url, pool_pre_ping=True)
    DB_PATH = Path.home() / "usb-assistant" / "usb_assistant.db"
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


engine = _build_engine()


# ── Models ────────────────────────────────────────────────────────────────────

class User(SQLModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    password_hash: str
    display_name: str = Field(default="")
    role: str = Field(default="user")          # "user" | "admin"
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_login_at: Optional[datetime] = Field(default=None)


class Merchant(SQLModel, table=True):
    __tablename__ = "merchants"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    business_type: str = Field(default="")
    address: str = Field(default="")
    meta_json: str = Field(default="")  # JSON blob: place_ids, store_count, chain_kb, etc.
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UserMerchant(SQLModel, table=True):
    __tablename__ = "user_merchants"

    user_id: int = Field(foreign_key="users.id", primary_key=True)
    merchant_id: int = Field(foreign_key="merchants.id", primary_key=True)
    role: str = Field(default="owner")


class Conversation(SQLModel, table=True):
    __tablename__ = "conversations"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    merchant_id: Optional[int] = Field(default=None, foreign_key="merchants.id")
    title: str = Field(default="")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    is_archived: bool = Field(default=False)


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversations.id")
    role: str                          # "user" | "assistant" | "summary"
    content: str
    canvas_json: Optional[str] = Field(default=None)  # JSON array of canvasUpdate events
    created_at: datetime = Field(default_factory=datetime.utcnow)


class KBDocument(SQLModel, table=True):
    __tablename__ = "kb_documents"

    id: Optional[int] = Field(default=None, primary_key=True)
    merchant_id: int = Field(foreign_key="merchants.id")
    filename: str                      # server-side stored name
    original_name: str                 # user-facing name
    file_hash: Optional[str] = Field(default=None)  # MD5 for dedup
    status: str = Field(default="processing")   # processing | indexed | failed
    chunk_count: int = Field(default=0)
    file_size: int = Field(default=0)
    uploaded_by: Optional[int] = Field(default=None, foreign_key="users.id")
    uploaded_at: datetime = Field(default_factory=datetime.utcnow)
    indexed_at: Optional[datetime] = Field(default=None)


class PlatformKBDocument(SQLModel, table=True):
    __tablename__ = "platform_kb_documents"

    id: Optional[int] = Field(default=None, primary_key=True)
    filename: str
    original_name: str
    status: str = Field(default="processing")   # processing | indexed | failed
    chunk_count: int = Field(default=0)
    uploaded_by: Optional[int] = Field(default=None, foreign_key="users.id")
    uploaded_at: datetime = Field(default_factory=datetime.utcnow)
    indexed_at: Optional[datetime] = Field(default=None)


class SystemPrompt(SQLModel, table=True):
    __tablename__ = "system_prompts"

    id: Optional[int] = Field(default=None, primary_key=True)
    version: int
    content: str
    status: str = Field(default="draft")       # draft | testing | active | archived
    label: str = Field(default="")
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    published_at: Optional[datetime] = Field(default=None)
    test_result: str = Field(default="")


class RefreshToken(SQLModel, table=True):
    __tablename__ = "refresh_tokens"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    token_hash: str
    expires_at: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LLMConfig(SQLModel, table=True):
    """Active LLM provider configuration, managed via admin panel."""
    __tablename__ = "llm_configs"

    id: Optional[int] = Field(default=None, primary_key=True)
    provider: str                           # claude | openai
    model: str = Field(default="")          # model name override
    api_key: str = Field(default="")        # encrypted at rest in production
    is_active: bool = Field(default=False)  # only one row should be active
    label: str = Field(default="")          # e.g. "Gemini Pro (production)"
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ── Session dependency ────────────────────────────────────────────────────────

def get_db():
    with Session(engine) as session:
        yield session


_DEFAULT_LLM_CONFIGS = [
    {"provider": "claude", "model": "claude-opus-4-7",   "label": "Claude Opus 4.7",  "is_active": True},
    {"provider": "claude", "model": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6","is_active": True},
    {"provider": "openai", "model": "gpt-4o",            "label": "GPT-4o",           "is_active": True},
    {"provider": "openai", "model": "o4-mini",           "label": "o4-mini",          "is_active": True},
]


def init_db():
    SQLModel.metadata.create_all(engine)
    # Seed default LLM configs if none exist
    with Session(engine) as session:
        existing = session.exec(__import__("sqlmodel", fromlist=["select"]).select(LLMConfig)).first()
        if not existing:
            for cfg in _DEFAULT_LLM_CONFIGS:
                session.add(LLMConfig(**cfg, updated_at=datetime.utcnow()))
            session.commit()
