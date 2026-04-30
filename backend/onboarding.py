from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from .auth import get_current_user
from .db import Merchant, UserMerchant, get_db
from .rag import ingest_text
from . import rag as _rag

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

GOOGLE_MAPS_KEY = os.environ.get("GOOGLE_MAPS_KEY", "")
CLAUDE_API_BASE = "https://api.anthropic.com/v1"
CLAUDE_MODEL = "claude-sonnet-4-6"

_CHAINS_PATH = Path(__file__).parent / "chains.json"
_chains_cache: Optional[list[dict]] = None


def _load_chains() -> list[dict]:
    global _chains_cache
    if _chains_cache is None:
        try:
            data = json.loads(_CHAINS_PATH.read_text(encoding="utf-8"))
            _chains_cache = data.get("brands", [])
        except Exception:
            _chains_cache = []
    return _chains_cache


def _match_chain(query: str) -> Optional[dict]:
    q = query.strip().lower()
    for brand in _load_chains():
        if brand.get("name", "").lower() == q:
            return brand
        for alias in brand.get("aliases", []):
            if alias.lower() == q:
                return brand
    # partial match fallback
    for brand in _load_chains():
        if q in brand.get("name", "").lower():
            return brand
    return None


# ── /onboarding/search ──────────────────────────────────────────────────────

@router.get("/search")
async def search_brand(
    q: str,
    user=Depends(get_current_user),
):
    """Search brand via Google Places + local chains KB."""
    if not q or len(q.strip()) < 1:
        return {"candidates": []}

    chain_match = _match_chain(q)
    candidates: list[dict] = []

    google_key = os.environ.get("GOOGLE_MAPS_KEY", "")
    if google_key:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    "https://places.googleapis.com/v1/places:searchText",
                    json={
                        "textQuery": f"{q} Indonesia",
                        "maxResultCount": 15,
                        "languageCode": "en",
                        "locationRestriction": {
                            "rectangle": {
                                "low": {"latitude": -11.0, "longitude": 95.0},
                                "high": {"latitude": 6.0, "longitude": 141.0},
                            }
                        },
                    },
                    headers={
                        "Content-Type": "application/json",
                        "X-Goog-Api-Key": google_key,
                        "X-Goog-FieldMask": "places.id,places.displayName,places.rating,places.userRatingCount,places.priceLevel,places.formattedAddress,places.photos",
                    },
                )
            if res.status_code == 200:
                raw_places = res.json().get("places", [])
                candidates = _aggregate_places(raw_places, q, chain_match, google_key)
        except Exception as e:
            print(f"[onboarding/search] Google Places error: {e}")

    # If Google fails or returns nothing, fall back to chains KB only
    if not candidates and chain_match:
        candidates = [{
            "name": chain_match["name"],
            "rating": 0,
            "review_count": 0,
            "store_count": chain_match.get("storeCount", 1),
            "addresses": [],
            "place_ids": [],
            "photo_url": None,
            "chain_kb": _chain_kb_fields(chain_match),
        }]

    return {"candidates": candidates}


def _aggregate_places(raw: list[dict], query: str, chain_match: Optional[dict], google_key: str = "") -> list[dict]:
    """Group chain places together; return individual places as separate candidates."""
    q_lower = query.strip().lower()
    merge_keys: set[str] = {q_lower}
    if chain_match:
        merge_keys.add(chain_match["name"].lower())
        for alias in chain_match.get("aliases", []):
            merge_keys.add(alias.lower())

    chain_entries: list[dict] = []
    individual: list[dict] = []

    for p in raw:
        name = p.get("displayName", {}).get("text", "").strip()
        if not name:
            continue
        n_lower = name.lower()
        photo_ref = (p.get("photos") or [{}])[0].get("name")

        entry = {
            "place_id": p.get("id", ""),
            "name": name,
            "rating": p.get("rating", 0) or 0,
            "review_count": p.get("userRatingCount", 0) or 0,
            "address": p.get("formattedAddress", ""),
            "photo_name": photo_ref,
        }

        is_chain_match = any(mk in n_lower or n_lower in mk for mk in merge_keys)
        if chain_match and is_chain_match:
            chain_entries.append(entry)
        else:
            individual.append(entry)

    results: list[dict] = []

    # Aggregate all chain locations into one candidate
    if chain_entries:
        best = max(chain_entries, key=lambda e: e["review_count"])
        photo_url = (
            f"https://places.googleapis.com/v1/{best['photo_name']}/media?maxHeightPx=200&key={google_key}"
            if best["photo_name"] else None
        )
        cand: dict = {
            "name": chain_match["name"],
            "rating": round(sum(e["rating"] for e in chain_entries) / len(chain_entries), 1),
            "review_count": sum(e["review_count"] for e in chain_entries),
            "store_count": len(chain_entries),
            "addresses": [e["address"] for e in chain_entries[:3]],
            "place_ids": [e["place_id"] for e in chain_entries],
            "photo_url": photo_url,
            "merchant_type": "existing",
            "chain_kb": _chain_kb_fields(chain_match),
        }
        results.append(cand)

    # Each individual (non-chain) place is its own candidate
    for entry in individual[:8]:
        photo_url = (
            f"https://places.googleapis.com/v1/{entry['photo_name']}/media?maxHeightPx=200&key={google_key}"
            if entry["photo_name"] else None
        )
        results.append({
            "name": entry["name"],
            "rating": entry["rating"],
            "review_count": entry["review_count"],
            "store_count": 1,
            "addresses": [entry["address"]],
            "place_ids": [entry["place_id"]],
            "photo_url": photo_url,
            "merchant_type": "existing",
        })

    return results


def _chain_kb_fields(brand: dict) -> dict:
    return {
        "category": brand.get("category"),
        "storeCount": brand.get("storeCount"),
        "storeCountAsOf": brand.get("storeCountAsOf"),
        "priceBand": brand.get("priceBand"),
        "founded": brand.get("founded"),
        "founder": brand.get("founder"),
        "markets": brand.get("markets"),
        "expansion": brand.get("expansion"),
    }


# ── /onboarding/bind ────────────────────────────────────────────────────────

class BindRequest(BaseModel):
    name: str
    place_ids: list[str] = []
    store_count: int = 1
    rating: float = 0
    review_count: int = 0
    addresses: list[str] = []
    chain_kb: Optional[dict] = None
    merchant_type: str = "existing"  # "existing" | "new"


@router.post("/bind")
def bind_merchant(
    req: BindRequest,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create or update the merchant bound to this user."""
    # Check existing binding
    existing_um = db.exec(select(UserMerchant).where(UserMerchant.user_id == user.id)).first()

    meta = {
        "place_ids": req.place_ids,
        "store_count": req.store_count,
        "rating": req.rating,
        "review_count": req.review_count,
        "addresses": req.addresses,
        "merchant_type": req.merchant_type,
    }
    if req.chain_kb:
        meta["chain_kb"] = req.chain_kb

    if existing_um:
        merchant = db.get(Merchant, existing_um.merchant_id)
        if merchant:
            brand_changed = merchant.name != req.name
            merchant.name = req.name
            merchant.meta_json = json.dumps(meta, ensure_ascii=False)
            db.add(merchant)
            db.commit()
            db.refresh(merchant)
            # Clear KB only when brand actually changes, and don't block on failure
            if brand_changed:
                try:
                    old_shop_id = f"u{user.id}_m{merchant.id}"
                    _rag.clear_shop_kb(old_shop_id)
                except Exception:
                    pass
            return {"merchant_id": merchant.id, "name": merchant.name}

    # Create new merchant
    merchant = Merchant(
        name=req.name,
        meta_json=json.dumps(meta, ensure_ascii=False),
    )
    db.add(merchant)
    db.flush()
    db.add(UserMerchant(user_id=user.id, merchant_id=merchant.id, role="owner"))
    db.commit()
    db.refresh(merchant)
    return {"merchant_id": merchant.id, "name": merchant.name}


# ── /onboarding/generate-kb ─────────────────────────────────────────────────

class GenerateKBRequest(BaseModel):
    merchant_id: int
    brand_name: str
    merchant_type: str = "existing"
    chain_kb: Optional[dict] = None


@router.post("/generate-kb")
async def generate_kb(
    req: GenerateKBRequest,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stream SSE: use Gemini with web grounding to generate initial KB."""
    # Verify user owns this merchant
    um = db.exec(
        select(UserMerchant).where(
            UserMerchant.user_id == user.id,
            UserMerchant.merchant_id == req.merchant_id,
        )
    ).first()
    if not um:
        raise HTTPException(status_code=403, detail="No access to this merchant")

    shop_id = f"u{user.id}_m{req.merchant_id}"

    # Clear any existing KB for this merchant before regenerating
    await _rag.clear_shop_kb_async(shop_id)

    async def _stream():
        yield _sse("status", {"msg": f"正在分析 {req.brand_name} 的品牌信息..."})

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            yield _sse("error", {"msg": "Anthropic API key not configured"})
            return

        # Build prompt
        chain_ctx = ""
        if req.chain_kb:
            parts = []
            if req.chain_kb.get("category"):
                parts.append(f"业态：{req.chain_kb['category']}")
            if req.chain_kb.get("storeCount"):
                parts.append(f"门店数：{req.chain_kb['storeCount']}（截至 {req.chain_kb.get('storeCountAsOf','')}）")
            if req.chain_kb.get("priceBand"):
                parts.append(f"价格带：{req.chain_kb['priceBand']}")
            if req.chain_kb.get("founded"):
                parts.append(f"成立时间：{req.chain_kb['founded']}")
            if req.chain_kb.get("expansion"):
                parts.append(f"扩张策略：{req.chain_kb['expansion']}")
            chain_ctx = "\n已知信息：" + "；".join(parts)

        prompt = f"""你是「商户助手」平台的 AI 顾问。一位商家刚刚完成注册并绑定了自己的品牌，这是平台对其品牌的**首次自动扫描**。请基于网络公开信息，生成一份品牌档案报告，供商家在平台内使用。{chain_ctx}

品牌名：{req.brand_name}
商家类型：{"连锁品牌" if req.merchant_type == "existing" else "新品牌/独立门店"}

请严格按以下结构输出，每个部分用 ## 标题分隔，第一个模块必须是平台扫描摘要。
**重要格式要求：只使用 bullet 列表（- 开头），不要使用 markdown 表格（|）。**

## 平台扫描摘要
用 3 句话（总字数不超过 100 字），以「商户助手」平台顾问的口吻：
- 第1句：品牌当前所处阶段和核心优势
- 第2句：当前面临的最主要挑战或机会
- 第3句：平台接下来能帮到的 1-2 个具体方向
每句单独成行，语气专业亲切。

## 品牌概况
用 bullet 列表输出：品牌全称、品类、成立背景、核心定位、目标客群。

## 产品与菜单
用 bullet 列表输出：核心产品线、招牌产品、价格带（Rp）、特色卖点。

## 市场表现
用 bullet 列表输出：在印尼的门店数量和分布、扩张速度、市场份额、消费者口碑。

## 竞争格局
用 bullet 列表输出：主要竞争对手（逐条列出）、TOMORO 的差异化优势、主要风险。

## 经营特点
用 bullet 列表输出：运营模式、供应链特点、数字化/外卖平台表现。

## 营销策略
用 bullet 列表输出：社交媒体运营、促销活动、品牌合作案例。

请尽量基于网络上可查到的真实信息，信息不确定时注明「数据待核实」。"""

        full_text = ""
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                async with client.stream(
                    "POST", f"{CLAUDE_API_BASE}/messages",
                    json={
                        "model": CLAUDE_MODEL,
                        "max_tokens": 4096,
                        "temperature": 0.3,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": True,
                    },
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        print(f"[generate-kb] Claude error {resp.status_code}: {body[:200]}")
                        yield _sse("error", {"msg": f"Claude error {resp.status_code}"})
                        return
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            raw = line[6:].strip()
                            try:
                                obj = json.loads(raw)
                                if obj.get("type") == "content_block_delta":
                                    chunk = obj.get("delta", {}).get("text", "")
                                    if chunk:
                                        full_text += chunk
                                        yield _sse("chunk", {"text": chunk})
                            except Exception:
                                pass
        except Exception as e:
            yield _sse("error", {"msg": str(e)})
            return

        if not full_text.strip():
            yield _sse("error", {"msg": "未能获取品牌信息，请稍后重试"})
            return

        yield _sse("status", {"msg": "正在写入知识库..."})

        # Ingest into ChromaDB
        try:
            doc_text = f"# {req.brand_name} 品牌知识库\n\n{full_text}"
            await ingest_text(
                text=doc_text,
                shop_id=shop_id,
                source=f"onboarding:{req.brand_name}",
            )
            yield _sse("done", {"msg": "知识库生成完成", "chars": len(full_text)})
        except Exception as e:
            yield _sse("error", {"msg": f"写入知识库失败：{e}"})

    return StreamingResponse(_stream(), media_type="text/event-stream")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
