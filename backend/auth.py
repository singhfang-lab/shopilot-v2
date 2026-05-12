from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlmodel import Session, select

from .db import Merchant, RefreshToken, User, UserMerchant, get_db  # noqa: F401

router = APIRouter(prefix="/auth", tags=["auth"])

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-in-production")
ACCESS_TTL = timedelta(hours=8)
REFRESH_TTL = timedelta(days=30)
ALGORITHM = "HS256"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _create_access_token(user_id: int, role: str) -> str:
    exp = datetime.now(timezone.utc) + ACCESS_TTL
    return jwt.encode({"sub": str(user_id), "role": role, "exp": exp}, JWT_SECRET, algorithm=ALGORITHM)


def _create_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _set_cookies(response: Response, access_token: str, refresh_token: str, admin: bool = False) -> None:
    prefix = "admin_" if admin else ""
    response.set_cookie(
        f"{prefix}access_token", access_token,
        httponly=True, samesite="strict", max_age=int(ACCESS_TTL.total_seconds()),
    )
    response.set_cookie(
        f"{prefix}refresh_token", refresh_token,
        httponly=True, samesite="strict", max_age=int(REFRESH_TTL.total_seconds()),
        path=f"/auth/{'admin-' if admin else ''}refresh",
    )


def _clear_cookies(response: Response, admin: bool = False) -> None:
    prefix = "admin_" if admin else ""
    response.delete_cookie(f"{prefix}access_token")
    response.delete_cookie(f"{prefix}refresh_token", path=f"/auth/{'admin-' if admin else ''}refresh")


# ── FastAPI dependencies ──────────────────────────────────────────────────────

def _decode_token(token: str) -> Optional[int]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        return int(payload["sub"])
    except Exception:
        return None


def get_current_user(
    access_token: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    credentials_exc = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    if not access_token:
        raise credentials_exc
    user_id = _decode_token(access_token)
    if user_id is None:
        raise credentials_exc
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise credentials_exc
    return user


def get_current_user_optional(
    access_token: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_db),
) -> Optional[User]:
    if not access_token:
        return None
    user_id = _decode_token(access_token)
    if user_id is None:
        return None
    user = db.get(User, user_id)
    return user if (user and user.is_active) else None


def get_admin_user(
    admin_access_token: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    """Reads admin_access_token cookie — independent from the user session."""
    credentials_exc = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    if not admin_access_token:
        raise credentials_exc
    user_id = _decode_token(admin_access_token)
    if user_id is None:
        raise credentials_exc
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise credentials_exc
    return user


def require_admin(user: User = Depends(get_admin_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


def get_merchant_for_user(user: User, db: Session) -> Optional[Merchant]:
    um = db.exec(select(UserMerchant).where(UserMerchant.user_id == user.id)).first()
    if not um:
        return None
    return db.get(Merchant, um.merchant_id)


# ── Schemas ───────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/register", status_code=201)
def register(req: RegisterRequest, response: Response, db: Session = Depends(get_db)):
    if db.exec(select(User).where(User.email == req.email)).first():
        raise HTTPException(status_code=400, detail="该邮箱已注册")

    user = User(
        email=req.email,
        password_hash=_hash_password(req.password),
        display_name=req.display_name or req.email.split("@")[0],
    )
    db.add(user)
    db.flush()

    access_token = _create_access_token(user.id, user.role)
    refresh_token = _create_refresh_token()
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=_hash_token(refresh_token),
        expires_at=datetime.now(timezone.utc) + REFRESH_TTL,
    ))
    db.commit()

    _set_cookies(response, access_token, refresh_token)
    return {"user_id": user.id, "email": user.email, "display_name": user.display_name}


@router.post("/login")
def login(req: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.exec(select(User).where(User.email == req.email)).first()
    if not user or not _verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

    user.last_login_at = datetime.now(timezone.utc)
    db.add(user)

    access_token = _create_access_token(user.id, user.role)
    refresh_token = _create_refresh_token()
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=_hash_token(refresh_token),
        expires_at=datetime.now(timezone.utc) + REFRESH_TTL,
    ))
    db.commit()

    _set_cookies(response, access_token, refresh_token)

    merchant = get_merchant_for_user(user, db)
    return {
        "user_id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "merchant": {"id": merchant.id, "name": merchant.name} if merchant else None,
    }


@router.post("/logout")
def logout(
    response: Response,
    user: User = Depends(get_current_user),
    refresh_token: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_db),
):
    if refresh_token:
        token_hash = _hash_token(refresh_token)
        stored = db.exec(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        ).first()
        if stored:
            db.delete(stored)
    else:
        # No cookie present — clear all sessions for this user as fallback
        for t in db.exec(select(RefreshToken).where(RefreshToken.user_id == user.id)).all():
            db.delete(t)
    db.commit()
    _clear_cookies(response, admin=False)
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    import json as _json
    merchant = get_merchant_for_user(user, db)
    merchant_data = None
    if merchant:
        try:
            meta = _json.loads(merchant.meta_json or "{}")
        except Exception:
            meta = {}
        merchant_data = {
            "id": merchant.id,
            "name": merchant.name,
            "business_type": merchant.business_type,
            "address": merchant.address,
            "region": meta.get("region", "id"),
        }
    return {
        "user_id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "merchant": merchant_data,
    }


# ── Admin-specific session endpoints ─────────────────────────────────────────

@router.post("/admin-login")
def admin_login(req: LoginRequest, response: Response, db: Session = Depends(get_db)):
    """Login for admin console — sets admin_access_token cookie, leaves user session untouched."""
    user = db.exec(select(User).where(User.email == req.email)).first()
    if not user or not _verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    user.last_login_at = datetime.now(timezone.utc)
    db.add(user)
    access_token = _create_access_token(user.id, user.role)
    refresh_token = _create_refresh_token()
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=_hash_token(refresh_token),
        expires_at=datetime.now(timezone.utc) + REFRESH_TTL,
    ))
    db.commit()
    _set_cookies(response, access_token, refresh_token, admin=True)
    return {"user_id": user.id, "email": user.email, "role": user.role}


@router.post("/admin-logout")
def admin_logout(response: Response, user: User = Depends(get_admin_user), db: Session = Depends(get_db)):
    _clear_cookies(response, admin=True)
    return {"ok": True}


@router.get("/admin-me")
def admin_me(user: User = Depends(get_admin_user)):
    return {"user_id": user.id, "email": user.email, "role": user.role}


@router.post("/refresh")
def refresh_token_endpoint(
    response: Response,
    refresh_token: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_db),
):
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token")

    token_hash = _hash_token(refresh_token)
    stored = db.exec(
        select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.expires_at > datetime.now(timezone.utc),
        )
    ).first()

    if not stored:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    user = db.get(User, stored.user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found")

    # Rotate refresh token
    db.delete(stored)
    new_access = _create_access_token(user.id, user.role)
    new_refresh = _create_refresh_token()
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=_hash_token(new_refresh),
        expires_at=datetime.now(timezone.utc) + REFRESH_TTL,
    ))
    db.commit()

    _set_cookies(response, new_access, new_refresh)
    return {"ok": True}
